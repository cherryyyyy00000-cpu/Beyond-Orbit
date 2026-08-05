"""Free neural narration via edge-tts, with word-level timings.

edge-tts drives Microsoft Edge's online neural voices. It is completely free,
needs NO API key, and the US "Andrew"/"Brian" voices sound genuinely
documentary-grade — which is the whole reason this channel can exist at zero
cost.

Two things come out of one synthesis pass:

1. an MP3 of the narration, and
2. **word-level timings**, which everything downstream depends on:
     * ``modules/captions.py``      -> the .srt caption track and the Short's
                                       karaoke subtitles,
     * ``modules/script_writer.py`` -> chapter timestamps,
     * ``modules/shorts_clipper.py``-> finding a clean sentence boundary for the
                                       40-second Short.

Chunked by design
-----------------
A 12-minute documentary is roughly 1,800 words. Sending that as one request is
a single point of failure: one network hiccup and the whole render is wasted.
So narration is synthesized **per paragraph**, each chunk is retried
independently, and the parts are concatenated with ffmpeg. Word timings from
each chunk are shifted by the measured duration of everything before it, so the
final timeline is continuous.

Public API
----------
    voice          = pick_voice()
    audio, words   = synthesize(text, out_mp3)
    audio, words   = synthesize_blocks(["para one", "para two"], out_mp3)
"""

from __future__ import annotations

import asyncio
import random
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from modules.config import cfg, setup_logging

log = setup_logging(__name__)

_TICKS_PER_SEC = 10_000_000  # edge-tts offsets are in 100-nanosecond ticks
_MIN_AUDIO_BYTES = 800
_MAX_CHUNK_CHARS = 1800      # keep each request comfortably small
_RETRIES = 3


# ---------------------------------------------------------------------------
# Voice selection
# ---------------------------------------------------------------------------
def pick_voice() -> str:
    """Return the narrator voice.

    Note ``tts.rotate_voices`` defaults to FALSE here, the opposite of a Shorts
    channel. A documentary channel's narrator IS part of its identity — swapping
    voices between uploads makes it feel like a content farm.
    """
    default = str(cfg("tts.voice", "en-US-AndrewNeural"))
    if bool(cfg("tts.rotate_voices", False)):
        pool = cfg("tts.voices", []) or [default]
        return random.choice(pool)
    return default


# ---------------------------------------------------------------------------
# ffprobe / ffmpeg helpers
# ---------------------------------------------------------------------------
def audio_duration(path: Path) -> float:
    """Duration in seconds, or 0.0 if it cannot be read."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, timeout=60,
        )
        return float(r.stdout.decode().strip() or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def _concat_audio(parts: Sequence[Path], out_path: Path) -> bool:
    """Concatenate MP3 parts losslessly using ffmpeg's concat demuxer."""
    if not parts:
        return False
    if len(parts) == 1:
        try:
            if parts[0].resolve() != out_path.resolve():
                out_path.write_bytes(parts[0].read_bytes())
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("Could not move single audio part into place: %s", exc)
            return False

    listing = out_path.with_suffix(".concat.txt")
    try:
        # The concat demuxer needs single quotes escaped as '\''.
        listing.write_text(
            "".join(f"file '{str(p.resolve()).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
                    for p in parts),
            encoding="utf-8",
        )
        r = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c", "copy", str(out_path)],
            capture_output=True, timeout=600,
        )
        if r.returncode != 0:
            # Stream copy can fail if the parts differ; re-encode as a fallback.
            log.warning("Lossless concat failed; re-encoding. %s",
                        r.stderr.decode(errors="ignore")[-300:])
            r = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "concat", "-safe", "0", "-i", str(listing),
                 "-c:a", "libmp3lame", "-b:a", "192k", str(out_path)],
                capture_output=True, timeout=900,
            )
        return r.returncode == 0 and out_path.exists()
    except Exception as exc:  # noqa: BLE001
        log.error("Audio concat failed: %s", exc)
        return False
    finally:
        listing.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
def _split_sentences(text: str) -> List[str]:
    """Split on sentence ends, keeping the punctuation."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _chunk_text(blocks: Sequence[str], max_chars: int = _MAX_CHUNK_CHARS) -> List[str]:
    """Group narration into synthesis-sized chunks without splitting sentences.

    Chunk boundaries land only between sentences, so no word is ever cut in half
    and the word timings stay trustworthy.
    """
    chunks: List[str] = []
    cur = ""
    for block in blocks:
        for sentence in _split_sentences(block):
            if len(sentence) > max_chars:
                # Pathologically long "sentence" — emit it alone rather than
                # silently dropping it.
                if cur:
                    chunks.append(cur.strip())
                    cur = ""
                chunks.append(sentence)
                continue
            if len(cur) + len(sentence) + 1 > max_chars and cur:
                chunks.append(cur.strip())
                cur = sentence
            else:
                cur = f"{cur} {sentence}".strip()
        # Prefer breaking at paragraph ends for natural pacing.
        if len(cur) > max_chars * 0.6:
            chunks.append(cur.strip())
            cur = ""
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------
async def _synth_chunk_async(text: str, out_path: Path, voice: str,
                             rate: str, volume: str, pitch: str) -> List[Dict]:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume, pitch=pitch)
    words: List[Dict] = []
    with open(out_path, "wb") as fh:
        async for chunk in communicate.stream():
            ctype = chunk.get("type")
            if ctype == "audio":
                fh.write(chunk["data"])
            elif ctype == "WordBoundary":
                start = chunk["offset"] / _TICKS_PER_SEC
                dur = chunk["duration"] / _TICKS_PER_SEC
                words.append({"start": start, "end": start + dur, "word": chunk["text"]})
    return words


def _synth_chunk(text: str, out_path: Path, voice: str, rate: str,
                 volume: str, pitch: str) -> Optional[List[Dict]]:
    """Synthesize one chunk with retries. Returns word timings, or None."""
    for attempt in range(1, _RETRIES + 1):
        try:
            words = asyncio.run(
                _synth_chunk_async(text, out_path, voice, rate, volume, pitch)
            )
            size = out_path.stat().st_size if out_path.exists() else 0
            if size >= _MIN_AUDIO_BYTES:
                return words
            log.warning("Chunk produced only %d bytes (attempt %d/%d).",
                        size, attempt, _RETRIES)
        except Exception as exc:  # noqa: BLE001
            log.warning("edge-tts chunk failed (attempt %d/%d): %s",
                        attempt, _RETRIES, exc)
        if attempt < _RETRIES:
            time.sleep(2 * attempt)  # simple backoff
    return None


def synthesize_blocks(
    blocks: Sequence[str],
    out_path,
    voice: Optional[str] = None,
    rate: Optional[str] = None,
    volume: Optional[str] = None,
    pitch: Optional[str] = None,
) -> Tuple[Optional[Path], List[Dict]]:
    """Narrate a list of text blocks into one MP3 with continuous word timings.

    Returns (mp3_path, words). On total failure returns (None, []) — the caller
    decides what to do rather than having an exception thrown at it mid-render.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    voice = voice or pick_voice()
    rate = rate if rate is not None else str(cfg("tts.rate", "+4%"))
    volume = volume if volume is not None else str(cfg("tts.volume", "+0%"))
    pitch = pitch if pitch is not None else str(cfg("tts.pitch", "+0Hz"))

    chunks = _chunk_text([b for b in blocks if str(b).strip()])
    if not chunks:
        log.error("Nothing to narrate — empty script.")
        return None, []

    log.info("Narrating %d chunk(s) with edge-tts (voice=%s, rate=%s)...",
             len(chunks), voice, rate)

    tmp_dir = out_path.parent
    part_paths: List[Path] = []
    all_words: List[Dict] = []
    offset = 0.0
    failed = 0

    for i, chunk in enumerate(chunks, start=1):
        part = tmp_dir / f"{out_path.stem}_part{i:03d}.mp3"
        words = _synth_chunk(chunk, part, voice, rate, volume, pitch)
        if words is None:
            failed += 1
            log.error("Chunk %d/%d could not be narrated — skipping its audio.",
                      i, len(chunks))
            part.unlink(missing_ok=True)
            continue

        # Shift this chunk's timings onto the global timeline.
        for w in words:
            all_words.append({
                "start": w["start"] + offset,
                "end": w["end"] + offset,
                "word": w["word"],
            })

        # Use the MEASURED duration, not the last word's end time — edge-tts
        # leaves trailing silence, and ignoring it would make every subsequent
        # chunk's timings drift progressively earlier.
        dur = audio_duration(part)
        if dur <= 0:
            dur = (words[-1]["end"] if words else 0.0) + 0.25
        offset += dur
        part_paths.append(part)
        log.info("  chunk %d/%d ok (%.1fs, %d words)", i, len(chunks), dur, len(words))

    if not part_paths:
        log.error("Every narration chunk failed — check network access to "
                  "edge-tts and that the voice name is valid.")
        return None, []
    if failed:
        log.warning("%d/%d chunk(s) failed; narration will have gaps in content.",
                    failed, len(chunks))

    ok = _concat_audio(part_paths, out_path)
    for p in part_paths:
        p.unlink(missing_ok=True)
    if not ok:
        log.error("Could not assemble the narration audio.")
        return None, []

    total = audio_duration(out_path)
    log.info("Narration ready: %s (%.1f min, %d word marks)",
             out_path.name, total / 60.0, len(all_words))
    return out_path, all_words


def synthesize(
    text: str,
    out_path,
    voice: Optional[str] = None,
    rate: Optional[str] = None,
    volume: Optional[str] = None,
    pitch: Optional[str] = None,
) -> Tuple[Optional[Path], List[Dict]]:
    """Convenience wrapper: narrate a single string (split on blank lines)."""
    blocks = [b for b in re.split(r"\n\s*\n", str(text)) if b.strip()] or [str(text)]
    return synthesize_blocks(blocks, out_path, voice, rate, volume, pitch)
