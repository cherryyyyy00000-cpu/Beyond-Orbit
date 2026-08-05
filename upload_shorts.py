#!/usr/bin/env python3
"""Beyond Orbit — upload the daily Shorts at US peak times.

Reads ``output/shorts_manifest.json``, uploads what is pending, and records the
video ids back into the manifest so ``finalize_rotation.py`` only retires items
that actually posted.

Every Short is uploaded in this run — that is where the API quota goes — but each
is given a ``publishAt`` from ``shorts.publish_hours_et`` (noon, 4 PM and 7 PM
Eastern by default) so the three spread across the US afternoon and evening
rather than all appearing at whatever time the runner happened to wake up.

Usage
-----
    python upload_shorts.py                # upload all pending
    python upload_shorts.py --limit 3
    python upload_shorts.py --now          # publish immediately, no scheduling
    python upload_shorts.py --privacy private
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from modules.config import OUTPUT_DIR, cfg, setup_logging

log = setup_logging("upload-shorts")

_UNITS_PER_UPLOAD = 1600
_DAILY_QUOTA = 10000


def manifest_path() -> Path:
    return OUTPUT_DIR / "shorts_manifest.json"


def load_manifest() -> dict:
    p = manifest_path()
    if not p.exists():
        return {"shorts": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.error("Could not read the Shorts manifest (%s).", exc)
        return {"shorts": []}


def save_manifest(manifest: dict) -> None:
    manifest_path().write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Upload Beyond Orbit Shorts.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--now", action="store_true",
                        help="publish immediately instead of at peak times")
    parser.add_argument("--privacy", type=str, default=None,
                        choices=["public", "unlisted", "private"])
    args = parser.parse_args(argv)

    manifest = load_manifest()
    pending = [s for s in manifest.get("shorts", []) if not s.get("uploaded")]
    if not pending:
        log.info("Nothing pending — run generate_shorts.py first.")
        return 0
    if args.limit:
        pending = pending[:args.limit]

    from modules import youtube

    # --- Quota guard -----------------------------------------------------
    # Warn up front rather than letting the last Short of the batch die
    # mid-upload with a confusing quotaExceeded error.
    planned = _UNITS_PER_UPLOAD * len(pending)
    log.info("Estimated API cost: %d units for %d Short(s) "
             "(daily quota %d; a documentary run also costs 2,050).",
             planned, len(pending), _DAILY_QUOTA)
    if planned > _DAILY_QUOTA:
        log.warning("This batch alone would exceed the daily quota — later "
                    "uploads will fail. Lower shorts.daily_count or use --limit.")

    try:
        service = youtube.build_service()
    except Exception as exc:  # noqa: BLE001
        log.error("Could not authenticate with YouTube: %s", exc)
        log.error("Run `python verify_setup.py` to diagnose.")
        return 1

    # All three land on real peak windows — first_immediate=False so none goes
    # out at an arbitrary runner wake-up time.
    slots = ([None] * len(pending) if args.now
             else youtube.next_short_publish_times(len(pending),
                                                   first_immediate=False))

    uploaded = 0
    for i, short in enumerate(pending):
        path = Path(short.get("video_path", ""))
        if not path.exists():
            log.error("Missing file for %s: %s", short.get("item_id"), path)
            continue

        log.info("=" * 66)
        vid = youtube.upload_video(
            path,
            title=short.get("title", "Beyond Orbit"),
            description=short.get("description", ""),
            tags=short.get("tags", []),
            privacy=args.privacy or "public",
            publish_at=slots[i] if i < len(slots) else None,
            service=service,
        )
        if not vid:
            log.error("Upload failed for %s.", short.get("item_id"))
            continue

        short["uploaded"] = True
        short["youtube_id"] = vid
        short["scheduled_publish_at"] = slots[i] if i < len(slots) else None
        uploaded += 1
        save_manifest(manifest)   # persist after each success

    save_manifest(manifest)

    log.info("=" * 66)
    log.info("Uploaded %d/%d Short(s).", uploaded, len(pending))
    for s in pending:
        if s.get("youtube_id"):
            log.info("  %s -> https://youtu.be/%s (publishes %s)",
                     str(s.get("heading", "?"))[:44], s["youtube_id"],
                     s.get("scheduled_publish_at") or "immediately")
    return 0 if uploaded else 1


if __name__ == "__main__":
    sys.exit(main())
