"""Cuts one vertical Short out of the finished documentary.

Why this module exists
---------------------
Shorts feed watch time does **not** count toward the 4,000-hour YPP threshold,
but long-form watch time does. So the two formats have different jobs:

    long-form  ->  the watch-hours engine
    Short      ->  the discovery engine that brings subscribers to it

Cutting the Short out of the documentary means one script, one narration and one
render produce both. The Short's description links back to the full video, so
they feed each other instead of competing.

Picking the segment
-------------------
Rather than blindly taking the first 40 seconds, candidate windows are scored on
how self-contained and hooky they are:

* they must START at a sentence boundary (a clip that opens mid-sentence feels
  broken and gets swiped away),
* concrete language scores well — numbers, comparisons, questions,
* the outro is excluded, because "subscribe for more" is a terrible Short opener,
* the cold open gets a bonus, since it was written to stop a scroll.

Public API
----------
    path = build_short(longform, words, out_path, hook=..., script=...)
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from modules.config import cfg, setup_logging

log = setup_logging(__name__)

# Words that signal a concrete, surprising claim — the kind of line that holds a
# scrolling viewer. Deliberately about specificity, not hype.
_HOOK_WORDS = {
    "billion", "million", "trillion", "thousand", "hundred",
    "never", "nobody", "impossible", "should", "cannot", "actually",
    "faster", "larger", "bigger", "smaller", "hotter", "colder", "older",
    "strange", "wrong", "unexplained", "unknown", "surprising", "expected",
    "times", "percent", "degrees", "kilometres", "kilometers", "miles",
    "light", "years", "seconds", "instantly", "forever",
}
_WEAK_WORDS = {"subscribe", "channel", "video", "chapters", "comment", "like"}

_SENTENCE_END = re.compile(r"[.!?]\"?$")


@dataclass
class Window:
    start: float
    end: float
    score: float
    text: str

    @property
    def duration(self) -> float:
        return self.end - self.start


# ---------------------------------------------------------------------------
# Segment selection
# ---------------------------------------------------------------------------
def _sentence_starts(words: Sequence[Dict]) -> List[int]:
    """Indices of words that begin a sentence."""
    starts = [0]
    for i, w in enumerate(words[:-1]):
        if _SENTENCE_END.search(str(w.get("word", "")).strip()):
            starts.append(i + 1)
    return starts


def _score_window(tokens: Sequence[str]) -> float:
    """Higher is better. Rewards concreteness, punishes housekeeping language."""
    if not tokens:
        return 0.0
    score = 0.0
    for raw in tokens:
        t = raw.lower().strip(".,!?\"'()")
        if t in _HOOK_WORDS:
            score += 2.0
        if t in _WEAK_WORDS:
            score -= 4.0
        if any(c.isdigit() for c in t):
            score += 1.5
    if any("?" in t for t in tokens):
        score += 2.0
    # Normalise so long windows are not automatically favoured.
    return score / max(1.0, len(tokens) / 40.0)


def _candidates(
    words: Sequence[Dict],
    target_seconds: float,
    total_duration: float,
) -> List[Window]:
    """All viable sentence-aligned windows, scored."""
    if not words:
        return []

    # Exclude the tail: it holds the outro and the subscribe ask.
    usable_end = total_duration * 0.88
    candidates: List[Window] = []

    for idx in _sentence_starts(words):
        start = float(words[idx]["start"])
        if start >= usable_end:
            break

        # Walk forward to the last full sentence that fits the target length.
        end_idx = idx
        for j in range(idx, len(words)):
            if float(words[j]["end"]) - start > target_seconds:
                break
            end_idx = j
        if end_idx <= idx:
            continue

        # Trim back to a sentence end so the clip closes cleanly.
        cut = end_idx
        for j in range(end_idx, idx, -1):
            if _SENTENCE_END.search(str(words[j].get("word", "")).strip()):
                cut = j
                break
        end = float(words[cut]["end"])
        if end - start < target_seconds * 0.55:
            continue

        tokens = [str(words[k].get("word", "")) for k in range(idx, cut + 1)]
        score = _score_window(tokens)
        if idx == 0:
            score += 6.0  # the cold open was engineered to stop a scroll
        candidates.append(Window(start=start, end=end, score=score,
                                 text=" ".join(tokens)))
    return candidates


def pick_window(
    words: Sequence[Dict],
    target_seconds: float,
    total_duration: float,
) -> Optional[Window]:
    """Choose the single best ~target_seconds window that starts on a sentence."""
    got = pick_windows(words, target_seconds, total_duration, count=1)
    return got[0] if got else None


def pick_windows(
    words: Sequence[Dict],
    target_seconds: float,
    total_duration: float,
    count: int = 1,
) -> List[Window]:
    """Choose up to ``count`` NON-OVERLAPPING windows, best first.

    Cutting several Shorts from one documentary is what makes daily Shorts
    affordable: three long-form uploads a week would otherwise yield only three
    Shorts a week, which is far too slow to build subscribers. Windows must not
    overlap, or the Shorts would obviously repeat the same lines.
    """
    candidates = _candidates(words, target_seconds, total_duration)
    if not candidates:
        end = min(target_seconds, total_duration * 0.9)
        return [Window(start=0.0, end=end, score=0.0, text="")]

    chosen: List[Window] = []
    for cand in sorted(candidates, key=lambda w: w.score, reverse=True):
        if len(chosen) >= max(1, count):
            break
        # Require a real gap so the clips do not share sentences.
        if any(cand.start < c.end + 2.0 and c.start < cand.end + 2.0 for c in chosen):
            continue
        chosen.append(cand)

    chosen.sort(key=lambda w: w.start)
    for i, w in enumerate(chosen, start=1):
        log.info("Short window %d/%d: %.1fs-%.1fs (%.1fs, score %.1f)",
                 i, len(chosen), w.start, w.end, w.duration, w.score)
        log.info("  opens with: %s", w.text[:100])
    if len(chosen) < count:
        log.info("Only %d non-overlapping window(s) available (wanted %d) — the "
                 "narration is not long enough for more.", len(chosen), count)
    return chosen


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
def _escape_sub_path(path: Path) -> str:
    p = str(path)
    return p.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _duration(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, timeout=60,
        )
        return float(r.stdout.decode().strip() or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def build_short(
    longform_path,
    words: Sequence[Dict],
    out_path,
    hook: Optional[str] = None,
    window: Optional[Window] = None,
) -> Optional[Path]:
    """Render the vertical Short.

    Args:
        longform_path: the finished 16:9 documentary.
        words: word timings on the LONG-FORM timeline (they get rebased here).
        out_path: destination .mp4.
        hook: big opening caption line.
        window: override the automatic segment choice.

    Returns the path, or None on failure. A failed Short must never fail the
    documentary — the caller treats it as optional.
    """
    if not bool(cfg("shorts.enabled", True)):
        log.info("Shorts are disabled in config — skipping.")
        return None

    longform_path = Path(longform_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not longform_path.exists():
        log.warning("Cannot build the Short — source video missing.")
        return None

    total = _duration(longform_path)
    if total <= 0:
        log.warning("Cannot read the source duration — skipping the Short.")
        return None

    SW = int(cfg("shorts.width", 1080))
    SH = int(cfg("shorts.height", 1920))
    target = float(cfg("shorts.clip_seconds", 40))
    # YouTube treats uploads up to 3 minutes as Shorts, but 60s is the practical
    # ceiling for retention on a clip like this.
    target = max(8.0, min(target, 60.0))

    win = window or pick_window(words, target, total)
    if not win:
        return None
    duration = min(win.duration, total - win.start)
    if duration < 6:
        log.warning("Chosen window is only %.1fs — skipping the Short.", duration)
        return None

    # Karaoke captions, rebased onto the clip's own timeline.
    ass_path = None
    try:
        from modules.captions import build_karaoke_ass
        ass_path = build_karaoke_ass(
            words,
            out_path.with_suffix(".ass"),
            SW, SH,
            hook=hook,
            cta=str(cfg("shorts.cta_text", "")) or None,
            time_offset=win.start,
            total_duration=duration,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Short captions failed (%s) — rendering without them.", exc)

    # Centre-crop 16:9 down to 9:16. For space footage the subject is almost
    # always central, so cropping reads better than pillarboxing with a blurred
    # copy of the same frame.
    vf = [f"crop=ih*{SW}/{SH}:ih", f"scale={SW}:{SH}", "setsar=1"]
    if ass_path and Path(ass_path).exists():
        vf.append(f"subtitles='{_escape_sub_path(Path(ass_path))}'")

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{win.start:.2f}", "-t", f"{duration:.2f}",
        "-i", str(longform_path),
        "-vf", ",".join(vf),
        "-c:v", "libx264", "-preset", str(cfg("video.preset", "fast")),
        "-crf", str(int(cfg("video.crf", 20))),
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-r", str(int(cfg("video.fps", 30))),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", str(out_path),
    ]

    log.info("Rendering Short: %dx%d, %.1fs from %.1fs in", SW, SH, duration, win.start)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=1800)
    except Exception as exc:  # noqa: BLE001
        log.warning("Short render error: %s", exc)
        return None
    finally:
        if ass_path:
            Path(ass_path).unlink(missing_ok=True)

    if r.returncode != 0 or not out_path.exists():
        log.warning("Short render failed: %s", r.stderr.decode(errors="ignore")[-300:])
        return None

    log.info("Short rendered: %s (%.1f MB)", out_path.name,
             out_path.stat().st_size / 1_048_576)
    return out_path
