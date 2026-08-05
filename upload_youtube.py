#!/usr/bin/env python3
"""Beyond Orbit — upload prepared videos to YouTube.

Reads ``output/manifest.json``, uploads anything not yet posted, and writes the
resulting video ids back into the manifest so ``finalize_rotation.py`` knows what
actually succeeded.

Scheduled publishing is the default. A 1440p/4K file is uploaded as **private
with ``publishAt``** so YouTube can finish HD processing before it goes live — a
video that goes public mid-processing serves 360p during its most important hour.
Use ``--now`` to bypass that for a test.

The Short is uploaded AFTER the documentary, so its description can link to the
real video URL — that link is the whole point of making the Short.

Usage
-----
    python upload_youtube.py                 # upload everything pending
    python upload_youtube.py --limit 1
    python upload_youtube.py --now           # publish immediately
    python upload_youtube.py --privacy private
    python upload_youtube.py --no-short
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from modules.config import OUTPUT_DIR, cfg, setup_logging

log = setup_logging("upload")


def manifest_path() -> Path:
    return OUTPUT_DIR / "manifest.json"


def load_manifest() -> dict:
    p = manifest_path()
    if not p.exists():
        return {"videos": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.error("Could not read the manifest (%s).", exc)
        return {"videos": []}


def save_manifest(manifest: dict) -> None:
    manifest_path().write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Upload Beyond Orbit videos.")
    parser.add_argument("--limit", type=int, default=None,
                        help="maximum number of documentaries to upload")
    parser.add_argument("--now", action="store_true",
                        help="publish immediately instead of scheduling")
    parser.add_argument("--privacy", type=str, default=None,
                        choices=["public", "unlisted", "private"])
    parser.add_argument("--no-short", action="store_true",
                        help="do not upload the accompanying Short")
    args = parser.parse_args(argv)

    manifest = load_manifest()
    videos = manifest.get("videos", [])
    pending = [v for v in videos if not v.get("uploaded_youtube")]

    if not pending:
        log.info("Nothing pending — run generate.py first.")
        return 0
    if args.limit:
        pending = pending[:args.limit]

    from modules import youtube

    # --- Quota guard ------------------------------------------------------
    # The daily YouTube quota is 10,000 units and a video upload alone costs
    # 1,600. Warn BEFORE starting rather than having the last Short of the batch
    # die with quotaExceeded halfway through.
    planned = 0
    for v in pending:
        planned += 1600 + (400 if v.get("caption_path") else 0) \
            + (50 if v.get("thumbnail_path") else 0)
        if not args.no_short:
            planned += 1600 * len(v.get("shorts") or [])
    log.info("Estimated API cost this run: %d units (daily quota 10,000).", planned)
    if planned > 10000:
        log.warning(
            "This run is planned to cost %d units, over the 10,000 daily quota — "
            "the later uploads will fail with quotaExceeded. Lower shorts.count "
            "in config.json, or use --limit to split the batch across days.",
            planned,
        )

    try:
        service = youtube.build_service()
    except Exception as exc:  # noqa: BLE001
        log.error("Could not authenticate with YouTube: %s", exc)
        log.error("Run `python verify_setup.py` to diagnose.")
        return 1

    publish_at = None if args.now else youtube.next_publish_time()
    if args.now:
        log.info("--now: publishing immediately (skipping the processing head "
                 "start; the first minutes may be served in low resolution).")

    uploaded = 0
    for entry in pending:
        video_path = Path(entry.get("video_path", ""))
        if not video_path.exists():
            log.error("Missing file for %r: %s", entry.get("title"), video_path)
            continue

        log.info("=" * 70)
        vid = youtube.upload_video(
            video_path,
            title=entry.get("title", entry.get("topic_title", "Beyond Orbit")),
            description=entry.get("description", ""),
            tags=entry.get("tags", []),
            privacy=args.privacy,
            publish_at=publish_at,
            thumbnail_path=(Path(entry["thumbnail_path"])
                            if entry.get("thumbnail_path") else None),
            caption_path=(Path(entry["caption_path"])
                          if entry.get("caption_path") else None),
            service=service,
        )
        if not vid:
            log.error("Documentary upload failed: %r", entry.get("title"))
            continue

        entry["uploaded_youtube"] = True
        entry["youtube_id"] = vid
        entry["scheduled_publish_at"] = publish_at
        uploaded += 1
        save_manifest(manifest)  # persist after each success

        # --- the Shorts, each linking back to the documentary -------------
        shorts = entry.get("shorts") or []
        if args.no_short or not shorts:
            continue
        try:
            import datetime as _dt

            from modules import metadata as meta_mod
            from modules.topic_source import get_topic_by_id

            topic = get_topic_by_id(entry.get("topic_id", ""))
            # Real peak-time slots rather than "run time + an hour".
            slots = ([None] * len(shorts) if args.now
                     else youtube.next_short_publish_times(len(shorts)))
            posted_n = 0

            for i, short in enumerate(shorts):
                sp = Path(short.get("path", ""))
                if short.get("uploaded") or not sp.exists():
                    continue

                if topic:
                    sm = meta_mod.build_short_metadata(
                        topic, entry.get("title", ""), parent_video_id=vid)
                    s_title, s_desc = sm.title, sm.description
                else:
                    s_title = entry.get("title", "Beyond Orbit")
                    s_desc = f"Full documentary: https://youtu.be/{vid}"
                # Number them so several Shorts from one film are distinguishable
                # in Studio and to viewers.
                if len(shorts) > 1:
                    s_title = f"{s_title} ({i + 1}/{len(shorts)})"[:100]

                # First Short goes out immediately; the rest land on the next
                # peak windows (shorts.publish_hours_et).
                s_publish = slots[i] if i < len(slots) else None

                short_id = youtube.upload_video(
                    sp,
                    title=s_title,
                    description=s_desc,
                    tags=entry.get("tags", []),
                    privacy=args.privacy or "public",
                    publish_at=s_publish,
                    service=service,
                )
                if short_id:
                    short["uploaded"] = True
                    short["youtube_id"] = short_id
                    short["scheduled_publish_at"] = s_publish
                    posted_n += 1
                    save_manifest(manifest)
            if posted_n:
                log.info("Uploaded %d/%d Short(s) for this documentary.",
                         posted_n, len(shorts))
        except Exception as exc:  # noqa: BLE001
            log.warning("Short upload failed (%s) — the documentary is fine.", exc)

    save_manifest(manifest)

    log.info("=" * 70)
    log.info("Uploaded %d/%d documentary(ies).", uploaded, len(pending))
    for v in pending:
        if v.get("youtube_id"):
            when = v.get("scheduled_publish_at") or "immediately"
            log.info("  %s -> https://youtu.be/%s (publishes %s)",
                     v.get("title", "?")[:52], v["youtube_id"], when)
            for s in (v.get("shorts") or []):
                if s.get("youtube_id"):
                    log.info("      short -> https://youtu.be/%s (%s)",
                             s["youtube_id"],
                             s.get("scheduled_publish_at") or "immediately")
    return 0 if uploaded else 1


if __name__ == "__main__":
    sys.exit(main())
