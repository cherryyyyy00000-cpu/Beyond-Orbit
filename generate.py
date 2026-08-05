#!/usr/bin/env python3
"""Beyond Orbit — build one space documentary (plus its Short).

Pipeline
--------
     1. pick the next unused topic from topics/space_bank.json
     2. write a 7-layer-hook script (Gemini if available, templates otherwise)
     3. GATE: refuse to publish a stub — see below
     4. narrate it with edge-tts and capture word timings
     5. build an .srt caption track and chapter timestamps
     6. fetch public-domain NASA footage matching the topic
     7. render the 16:9 documentary (1440p by default)
     8. build title / description / chapters / tags / attribution
     9. generate three thumbnail variants
    10. cut one vertical Short out of the documentary
    11. write output/manifest.json for the uploaders

The publishable gate
--------------------
Without ``GEMINI_API_KEY`` the offline template mode only produces about 2-3
minutes of narration, because the topic bank holds a research BRIEF rather than a
finished script. Publishing a 2-minute file as a "documentary" would drag average
view duration down and teach the algorithm the channel is low value — worse than
not uploading. So a short script is SKIPPED and the topic is left in the rotation
for a later run that has a key. Override with ``script.allow_short_publish``.

Nothing is marked as used here. ``finalize_rotation.py`` does that, and only for
videos that actually uploaded, so a failed upload never burns a topic.

Usage
-----
    python generate.py                      # one documentary
    python generate.py --topic t007         # a specific topic
    python generate.py --no-short           # skip the Short
    python generate.py --dry-run            # plan only, no render
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import traceback
from pathlib import Path
from typing import Dict, List, Optional

from modules.config import (CACHE_DIR, OUTPUT_DIR, STATE_FILE, cfg, ensure_dirs,
                            resolution, resolution_name, setup_logging)

log = setup_logging("generate")

_HISTORY_CAP = 50_000


# ---------------------------------------------------------------------------
# State + manifest
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if Path(STATE_FILE).exists():
        try:
            data = json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
            data.setdefault("used_topic_ids", [])
            data.setdefault("used_titles", [])
            return data
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read %s (%s) — starting fresh.", STATE_FILE, exc)
    return {"used_topic_ids": [], "used_titles": []}


def manifest_path() -> Path:
    return OUTPUT_DIR / "manifest.json"


def load_manifest() -> dict:
    p = manifest_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"generated_at": None, "videos": []}


def save_manifest(manifest: dict) -> None:
    manifest_path().parent.mkdir(parents=True, exist_ok=True)
    manifest_path().write_text(json.dumps(manifest, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# One documentary
# ---------------------------------------------------------------------------
def produce(topic_id: Optional[str] = None, make_short: bool = True,
            dry_run: bool = False) -> Optional[dict]:
    """Produce one documentary. Returns a manifest entry, or None."""
    from modules import metadata as meta_mod
    from modules import nasa_fetch, script_writer, tts
    from modules.captions import build_srt
    from modules.topic_source import get_next_topic, get_topic_by_id

    state = load_state()
    used_ids = set(state.get("used_topic_ids", []))
    used_titles = set(state.get("used_titles", []))

    # --- 1. topic --------------------------------------------------------
    topic = get_topic_by_id(topic_id) if topic_id else get_next_topic(used_ids)
    if not topic:
        return None

    log.info("=" * 70)
    log.info("TOPIC %s — %s", topic.id, topic.title)
    log.info("=" * 70)

    # --- 2. script -------------------------------------------------------
    script = script_writer.write_script(topic)

    # --- 3. publishable gate --------------------------------------------
    if not script.is_publishable:
        log.error(
            "SKIPPING %s: the script is only ~%.1f min, under the %.0f min "
            "publishable floor. The topic stays in the rotation. Set "
            "GEMINI_API_KEY for full-length scripts, or set "
            "script.allow_short_publish=true to publish anyway.",
            topic.id, script.estimated_minutes,
            float(cfg("script.min_publishable_minutes", 6)),
        )
        return None

    if dry_run:
        log.info("[dry-run] %d words, ~%.1f min, %d chapters planned, "
                 "resolution %s. Stopping before render.",
                 script.word_count, script.estimated_minutes,
                 len(script.blocks()), resolution_name())
        return None

    base = f"bo_{topic.id}"
    W, H = resolution()

    # --- 4. narration ----------------------------------------------------
    voice = tts.pick_voice()
    audio_path, words = tts.synthesize_blocks(
        [text for _, text in script.blocks()],
        OUTPUT_DIR / f"{base}.mp3",
        voice=voice,
    )
    if not audio_path:
        log.error("Narration failed for %s — aborting this video.", topic.id)
        return None
    audio_seconds = tts.audio_duration(audio_path)

    # --- 5. captions + chapters -----------------------------------------
    srt_path = build_srt(words, OUTPUT_DIR / f"{base}.srt")
    chapters = script_writer.chapter_timings(
        script, words, audio_seconds + float(cfg("video.outro_seconds", 6.0))
    )

    # --- 6. footage ------------------------------------------------------
    queries = list(topic.visual_queries) or ["space", "galaxy"]
    assets = nasa_fetch.fetch_assets(queries, dest_dir=Path(CACHE_DIR) / base)

    # --- 7. render -------------------------------------------------------
    from modules import video_builder

    video_path = video_builder.build_documentary(
        audio_path, assets, topic.title, OUTPUT_DIR / f"{base}.mp4",
    )
    if not video_path:
        log.error("Render failed for %s.", topic.id)
        return None

    # --- 8. metadata -----------------------------------------------------
    meta = meta_mod.build_metadata(topic, script, chapters, assets, used_titles)

    # --- 9. thumbnails ---------------------------------------------------
    thumbs: List[Path] = []
    try:
        from modules.thumbnail import generate_thumbnails
        thumbs = generate_thumbnails(video_path, meta.thumbnail_hook,
                                     OUTPUT_DIR, stem=base)
    except Exception as exc:  # noqa: BLE001
        log.warning("Thumbnail generation failed (%s) — continuing.", exc)

    # --- 10. Shorts ------------------------------------------------------
    # Several Shorts are cut from the one render. Three long-form uploads a week
    # would otherwise produce only three Shorts a week, which is far too slow to
    # build subscribers.
    shorts: List[dict] = []
    # shorts.count defaults to 0: Shorts now come from their own daily run
    # (generate_shorts.py), because uploading them alongside the documentary
    # capped the channel at ~1.7 Shorts/day on the 10,000-unit API quota.
    want = int(cfg("shorts.count", 0))
    if make_short and want > 0 and bool(cfg("shorts.enabled", True)):
        try:
            from modules.shorts_clipper import build_short, pick_windows

            target = float(cfg("shorts.clip_seconds", 40))
            windows = pick_windows(words, target, audio_seconds, count=want)
            for i, win in enumerate(windows, start=1):
                sp = build_short(
                    video_path, words,
                    OUTPUT_DIR / f"{base}_short{i}.mp4",
                    hook=meta.thumbnail_hook, window=win,
                )
                if sp:
                    shorts.append({
                        "path": str(sp),
                        "window": [round(win.start, 1), round(win.end, 1)],
                        "uploaded": False,
                        "youtube_id": None,
                    })
        except Exception as exc:  # noqa: BLE001
            log.warning("Short creation failed (%s) — the documentary is "
                        "unaffected.", exc)

    entry = {
        "topic_id": topic.id,
        "topic_title": topic.title,
        "angle": topic.angle,
        "script_source": script.source,
        "word_count": script.word_count,
        "voice": voice,
        "resolution": resolution_name(),
        "dimensions": f"{W}x{H}",
        "duration_seconds": round(audio_seconds, 1),
        "title": meta.title,
        "description": meta.description,
        "tags": meta.tags,
        "thumbnail_hook": meta.thumbnail_hook,
        "meta_source": meta.source,
        "chapters": [[round(t, 2), label] for t, label in chapters],
        "asset_count": len(assets),
        "asset_credits": nasa_fetch.attribution_lines(assets),
        "video_path": str(video_path),
        "caption_path": str(srt_path) if srt_path else None,
        "thumbnail_path": str(thumbs[0]) if thumbs else None,
        "thumbnail_variants": [str(t) for t in thumbs],
        "shorts": shorts,
        "uploaded_youtube": False,
        "youtube_id": None,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    (OUTPUT_DIR / f"{base}.json").write_text(json.dumps(entry, indent=2),
                                             encoding="utf-8")
    log.info("-" * 70)
    log.info("READY  %s", meta.title)
    log.info("  video   %s (%.1f min)", Path(video_path).name, audio_seconds / 60)
    log.info("  shorts  %d", len(shorts))
    log.info("  thumbs  %d variant(s)", len(thumbs))
    log.info("  chapters %d | assets %d | script %s",
             len(chapters), len(assets), script.source)
    return entry


# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Beyond Orbit documentary builder.")
    parser.add_argument("--count", type=int, default=1,
                        help="how many documentaries to build (default 1)")
    parser.add_argument("--topic", type=str, default=None,
                        help="build one specific topic id, e.g. t007")
    parser.add_argument("--no-short", action="store_true",
                        help="skip the vertical Short")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan and script only; do not render")
    args = parser.parse_args(argv)

    ensure_dirs()
    log.info("Beyond Orbit — building %d video(s) at %s",
             args.count, resolution_name())

    manifest = load_manifest()
    made: List[dict] = []

    for i in range(args.count):
        try:
            entry = produce(topic_id=args.topic, make_short=not args.no_short,
                            dry_run=args.dry_run)
            if entry:
                made.append(entry)
        except KeyboardInterrupt:
            log.warning("Interrupted.")
            break
        except Exception as exc:  # noqa: BLE001
            log.error("Video %d/%d failed: %s", i + 1, args.count, exc)
            log.debug("%s", traceback.format_exc())

    if made:
        manifest["videos"].extend(made)
        manifest["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        save_manifest(manifest)

    log.info("=" * 70)
    log.info("Done: %d/%d video(s) ready in %s", len(made), args.count, OUTPUT_DIR)
    if not made and not args.dry_run:
        log.error("Nothing was produced. Run `python verify_setup.py` to check "
                  "the setup — the usual causes are a missing GEMINI_API_KEY "
                  "(scripts too short), no ffmpeg, or an exhausted topic bank.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
