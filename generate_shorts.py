#!/usr/bin/env python3
"""Beyond Orbit — build standalone vertical Shorts.

Separate from ``generate.py`` on purpose, for a quota reason rather than a
stylistic one. A video upload costs 1,600 of the 10,000 daily API units, so a day
that posts a documentary (2,050 with its caption track and thumbnail) can afford
at most four more uploads. Cutting Shorts out of the documentary meant every
Short had to be uploaded on the documentary's own day, which capped the channel
at roughly 1.7 Shorts a day.

Giving Shorts their own daily run fixes that:

    documentary day  =  2,050 + 3 x 1,600  =  6,850 units
    Shorts-only day  =          3 x 1,600  =  4,800 units

Both sit comfortably under 10,000, and the channel gets a genuine 3 Shorts a day.

The content needs no new authoring: each of the 50 topics carries 4-7 beats, and
one beat is almost exactly 40 seconds of narration, so the bank already holds
about 250 Shorts (see ``modules/shorts_source.py``).

Usage
-----
    python generate_shorts.py                # 3 Shorts (shorts.daily_count)
    python generate_shorts.py --count 1
    python generate_shorts.py --item t007#b2 # one specific beat
    python generate_shorts.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import traceback
from pathlib import Path
from typing import List, Optional

from modules.config import (CACHE_DIR, OUTPUT_DIR, STATE_FILE, cfg, ensure_dirs,
                            setup_logging)

log = setup_logging("shorts")

_HISTORY_CAP = 50_000


# ---------------------------------------------------------------------------
# State + manifest
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if Path(STATE_FILE).exists():
        try:
            data = json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
            data.setdefault("used_short_ids", [])
            return data
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read %s (%s) — starting fresh.", STATE_FILE, exc)
    return {"used_topic_ids": [], "used_titles": [], "used_short_ids": []}


def manifest_path() -> Path:
    return OUTPUT_DIR / "shorts_manifest.json"


def load_manifest() -> dict:
    p = manifest_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"generated_at": None, "shorts": []}


def save_manifest(manifest: dict) -> None:
    manifest_path().parent.mkdir(parents=True, exist_ok=True)
    manifest_path().write_text(json.dumps(manifest, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# One Short
# ---------------------------------------------------------------------------
def produce_short(item, dry_run: bool = False) -> Optional[dict]:
    """Narrate, render and package one Short. Returns a manifest entry."""
    from modules import nasa_fetch, tts
    from modules.captions import build_karaoke_ass
    from modules.shorts_source import build_short_script

    parts = build_short_script(item)
    text = parts["text"]
    if not text.strip():
        log.warning("Short %s produced no script — skipping.", item.id)
        return None

    words_est = len(text.split())
    est_seconds = words_est / (float(cfg("script.words_per_minute", 150)) / 60.0)
    log.info("-" * 66)
    log.info("SHORT %s — %s", item.id, item.heading)
    log.info("  %d words (~%.0fs) | from %s", words_est, est_seconds, item.topic_id)

    if dry_run:
        log.info("  [dry-run] hook: %s", parts["caption_hook"])
        log.info("  [dry-run] opens: %s", parts["hook"][:100])
        return None

    base = f"bo_short_{item.id.replace('#', '_')}"
    SW = int(cfg("shorts.width", 1080))
    SH = int(cfg("shorts.height", 1920))

    # --- narration -------------------------------------------------------
    voice = tts.pick_voice()
    audio_path, timings = tts.synthesize(text, OUTPUT_DIR / f"{base}.mp3", voice=voice)
    if not audio_path:
        log.error("Narration failed for %s.", item.id)
        return None
    duration = tts.audio_duration(audio_path)
    if duration <= 3:
        log.warning("Narration for %s is only %.1fs — skipping.", item.id, duration)
        return None

    # --- burned-in karaoke captions --------------------------------------
    ass_path = build_karaoke_ass(
        timings, OUTPUT_DIR / f"{base}.ass", SW, SH,
        hook=parts["caption_hook"],
        cta=str(cfg("shorts.cta_text", "")) or None,
        time_offset=0.0,
        total_duration=duration + 0.6,
    )

    # --- footage ---------------------------------------------------------
    # A 40-second clip with a 4-8s shot cadence needs about 6-8 shots, so a
    # handful of assets is plenty — and keeps the run fast and the disk small.
    assets = nasa_fetch.fetch_assets(
        item.visual_queries or ["space"],
        want=int(cfg("shorts.assets_per_short", 5)),
        dest_dir=Path(CACHE_DIR) / base,
    )

    # --- render ----------------------------------------------------------
    from modules import video_builder

    video_path = video_builder.build_vertical(
        audio_path, assets, item.heading, OUTPUT_DIR / f"{base}.mp4",
        ass_path=ass_path,
        max_seconds=float(cfg("shorts.max_seconds", 62)),
    )
    if not video_path:
        log.error("Render failed for Short %s.", item.id)
        return None

    # --- metadata --------------------------------------------------------
    from modules import metadata as meta_mod
    from modules.topic_source import get_topic_by_id

    topic = get_topic_by_id(item.topic_id)
    if topic:
        # Pass the BEAT's heading and hook so each Short gets a distinct title —
        # deriving it from the topic alone made every beat share one title.
        meta = meta_mod.build_short_metadata(
            topic, item.topic_title,
            heading=item.heading, hook=parts["hook"],
        )
        title, description, tags = meta.title, meta.description, meta.tags
    else:
        suffix = str(cfg("shorts.title_suffix", "#shorts")).strip()
        title = f"{item.heading} {suffix}".strip()[:100]
        description = f"{parts['hook']}\n\n{cfg('channel.cta', '')}".strip()
        tags = list(item.tags)

    entry = {
        "item_id": item.id,
        "topic_id": item.topic_id,
        "topic_title": item.topic_title,
        "heading": item.heading,
        "voice": voice,
        "duration_seconds": round(duration, 1),
        "dimensions": f"{SW}x{SH}",
        "caption_hook": parts["caption_hook"],
        "title": title,
        "description": description,
        "tags": tags,
        "asset_count": len(assets),
        "asset_credits": nasa_fetch.attribution_lines(assets),
        "video_path": str(video_path),
        "uploaded": False,
        "youtube_id": None,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    # The .ass is a build artefact — the captions are already burned in.
    if ass_path:
        Path(ass_path).unlink(missing_ok=True)

    log.info("  READY %s (%.1fs, %.1f MB) — %r",
             Path(video_path).name, duration,
             Path(video_path).stat().st_size / 1_048_576, title)
    return entry


# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build Beyond Orbit Shorts.")
    parser.add_argument("--count", type=int, default=None,
                        help="how many Shorts to build (default shorts.daily_count)")
    parser.add_argument("--item", type=str, default=None,
                        help="build one specific item, e.g. t007#b2")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be built without rendering")
    args = parser.parse_args(argv)

    ensure_dirs()
    from modules.shorts_source import get_next_shorts, load_short_items

    count = int(args.count if args.count is not None
                else cfg("shorts.daily_count", 3))

    if args.item:
        items = [i for i in load_short_items() if i.id == args.item]
        if not items:
            log.error("Short item %r not found. Ids look like 't007#b2'.", args.item)
            return 1
    else:
        state = load_state()
        used = set(state.get("used_short_ids", []))
        pool_total = len(load_short_items())
        log.info("Beyond Orbit Shorts — %d requested (%d/%d items already used)",
                 count, len(used), pool_total)
        items = get_next_shorts(count, used_ids=used)

    if not items:
        log.error("No Short items available.")
        return 1

    manifest = load_manifest()
    made: List[dict] = []
    for item in items:
        try:
            entry = produce_short(item, dry_run=args.dry_run)
            if entry:
                made.append(entry)
        except KeyboardInterrupt:
            log.warning("Interrupted.")
            break
        except Exception as exc:  # noqa: BLE001
            log.error("Short %s failed: %s", item.id, exc)
            log.debug("%s", traceback.format_exc())

    if made:
        manifest["shorts"].extend(made)
        manifest["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        save_manifest(manifest)

    log.info("=" * 66)
    log.info("Done: %d/%d Short(s) ready in %s", len(made), len(items), OUTPUT_DIR)
    if not made and not args.dry_run:
        log.error("No Shorts were produced — check the edge-tts and ffmpeg logs above.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
