#!/usr/bin/env python3
"""Beyond Orbit — mark topics used, but ONLY for videos that actually uploaded.

This runs after ``upload_youtube.py`` and is the only place ``rotation_state.json``
is written.

The reason it is a separate step: if the topic were marked used at generation
time, then any upload failure — an expired token, exhausted quota, a network
blip — would silently burn that topic forever. You would lose a video and never
find out. Deferring until an upload is confirmed means a failed run simply
retries the same topic next time.

Usage
-----
    python finalize_rotation.py
    python finalize_rotation.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import List, Optional

from modules.config import OUTPUT_DIR, STATE_FILE, setup_logging

log = setup_logging("finalize")

_HISTORY_CAP = 50_000


def load_state() -> dict:
    p = Path(STATE_FILE)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            data.setdefault("used_topic_ids", [])
            data.setdefault("used_titles", [])
            return data
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read the rotation state (%s).", exc)
    return {"used_topic_ids": [], "used_titles": []}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Finalize the topic rotation.")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args(argv)

    manifest_file = OUTPUT_DIR / "manifest.json"
    if not manifest_file.exists():
        log.info("No manifest — nothing to finalize.")
        return 0

    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.error("Could not read the manifest (%s).", exc)
        return 1

    videos = manifest.get("videos", [])
    posted = [v for v in videos if v.get("uploaded_youtube") and v.get("youtube_id")]
    failed = [v for v in videos if not v.get("uploaded_youtube")]

    if failed:
        log.info("%d video(s) did NOT upload — their topics stay in the rotation "
                 "and will be retried:", len(failed))
        for v in failed:
            log.info("  %s (%s)", v.get("topic_id"), v.get("title", "?")[:56])

    if not posted:
        log.info("No successful uploads — the rotation state is unchanged.")
        return 0

    state = load_state()
    used_ids: List[str] = list(state.get("used_topic_ids", []))
    used_titles: List[str] = list(state.get("used_titles", []))

    added = 0
    for v in posted:
        tid = v.get("topic_id")
        if tid and tid not in used_ids:
            used_ids.append(tid)
            added += 1
        title = v.get("title")
        if title and title not in used_titles:
            used_titles.append(title)
        log.info("  marked used: %s -> https://youtu.be/%s (%s)",
                 tid, v.get("youtube_id"), v.get("title", "?")[:48])

    state["used_topic_ids"] = used_ids[-_HISTORY_CAP:]
    state["used_titles"] = used_titles[-_HISTORY_CAP:]
    state["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()

    if args.dry_run:
        log.info("[dry-run] would mark %d topic(s) used (total %d).",
                 added, len(used_ids))
        return 0

    Path(STATE_FILE).write_text(json.dumps(state, indent=2), encoding="utf-8")
    log.info("Rotation updated: +%d topic(s), %d used in total.", added, len(used_ids))

    # Warn before the bank runs dry rather than after.
    try:
        from modules.topic_source import load_topics
        total = len(load_topics())
        remaining = total - len(set(used_ids))
        if remaining <= 0:
            log.error("The topic bank is EXHAUSTED (%d/%d used). Add new topics "
                      "to topics/space_bank.json — letting the channel repeat "
                      "itself is what the inauthentic-content policy targets.",
                      len(set(used_ids)), total)
        elif remaining <= 8:
            log.warning("Only %d topic(s) left in the bank. Time to add more.",
                        remaining)
        else:
            log.info("%d/%d topics remaining.", remaining, total)
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
