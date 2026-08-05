#!/usr/bin/env python3
"""Beyond Orbit — retire content, but ONLY what actually uploaded.

Runs after the upload steps and is the only place ``rotation_state.json`` is
written. It handles both pipelines:

    output/manifest.json         documentaries  -> used_topic_ids
    output/shorts_manifest.json  daily Shorts   -> used_short_ids

The two are tracked separately on purpose. A Short is one beat of a documentary,
so publishing the Short must not consume the documentary's topic — the same
material legitimately appears in both formats.

Why this is a separate step at all: if content were marked used at generation
time, any upload failure — an expired token, exhausted quota, a network blip —
would silently burn it forever. You would lose a video and never find out.
Deferring until an upload is confirmed means a failed run simply retries.

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
from typing import List, Optional, Tuple

from modules.config import OUTPUT_DIR, STATE_FILE, setup_logging

log = setup_logging("finalize")

_HISTORY_CAP = 50_000


def load_state() -> dict:
    p = Path(STATE_FILE)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read the rotation state (%s).", exc)
            data = {}
    else:
        data = {}
    data.setdefault("used_topic_ids", [])
    data.setdefault("used_titles", [])
    data.setdefault("used_short_ids", [])
    return data


def _read(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.error("Could not read %s (%s).", path.name, exc)
        return None


def _finalize_documentaries(state: dict) -> Tuple[int, int]:
    """Record uploaded documentaries. Returns (added, failed)."""
    manifest = _read(OUTPUT_DIR / "manifest.json")
    if not manifest:
        return 0, 0

    videos = manifest.get("videos", [])
    posted = [v for v in videos if v.get("uploaded_youtube") and v.get("youtube_id")]
    failed = [v for v in videos if not v.get("uploaded_youtube")]

    for v in failed:
        log.info("  documentary NOT uploaded — topic %s stays in rotation (%s)",
                 v.get("topic_id"), str(v.get("title", "?"))[:52])

    used_ids: List[str] = state["used_topic_ids"]
    used_titles: List[str] = state["used_titles"]
    added = 0
    for v in posted:
        tid = v.get("topic_id")
        if tid and tid not in used_ids:
            used_ids.append(tid)
            added += 1
        title = v.get("title")
        if title and title not in used_titles:
            used_titles.append(title)
        log.info("  documentary used: %s -> https://youtu.be/%s",
                 tid, v.get("youtube_id"))
    return added, len(failed)


def _finalize_shorts(state: dict) -> Tuple[int, int]:
    """Record uploaded Shorts. Returns (added, failed)."""
    manifest = _read(OUTPUT_DIR / "shorts_manifest.json")
    if not manifest:
        return 0, 0

    shorts = manifest.get("shorts", [])
    posted = [s for s in shorts if s.get("uploaded") and s.get("youtube_id")]
    failed = [s for s in shorts if not s.get("uploaded")]

    for s in failed:
        log.info("  Short NOT uploaded — item %s stays in rotation",
                 s.get("item_id"))

    used: List[str] = state["used_short_ids"]
    added = 0
    for s in posted:
        iid = s.get("item_id")
        if iid and iid not in used:
            used.append(iid)
            added += 1
        log.info("  Short used: %s -> https://youtu.be/%s",
                 iid, s.get("youtube_id"))
    return added, len(failed)


def _warn_on_runway(state: dict) -> None:
    """Flag an approaching content shortage before it becomes a dead channel."""
    try:
        from modules.shorts_source import load_short_items
        from modules.topic_source import load_topics

        topics_total = len(load_topics())
        topics_left = topics_total - len(set(state["used_topic_ids"]))
        shorts_total = len(load_short_items())
        shorts_left = shorts_total - len(set(state["used_short_ids"]))

        log.info("Runway: %d/%d documentary topics and %d/%d Short items remain.",
                 topics_left, topics_total, shorts_left, shorts_total)

        # At 4 documentaries and 21 Shorts a week.
        if topics_left <= 0:
            log.error("Documentary topics are EXHAUSTED. Add topics to "
                      "topics/space_bank.json — repeating content is what the "
                      "inauthentic-content policy penalises.")
        elif topics_left <= 8:
            log.warning("Only %d documentary topic(s) left — about %.1f week(s) "
                        "at 4/week. Time to add more.", topics_left, topics_left / 4)

        if shorts_left <= 0:
            log.error("Short items are EXHAUSTED. Every new topic adds 4-7 more.")
        elif shorts_left <= 21:
            log.warning("Only %d Short item(s) left — about %.1f week(s) at "
                        "21/week.", shorts_left, shorts_left / 21)
    except Exception:  # noqa: BLE001
        pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Finalize the content rotation.")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args(argv)

    state = load_state()
    doc_added, doc_failed = _finalize_documentaries(state)
    short_added, short_failed = _finalize_shorts(state)

    if doc_added == 0 and short_added == 0:
        log.info("No successful uploads to record — rotation state unchanged. "
                 "(%d documentary and %d Short upload(s) failed and will retry.)",
                 doc_failed, short_failed)
        return 0

    state["used_topic_ids"] = state["used_topic_ids"][-_HISTORY_CAP:]
    state["used_titles"] = state["used_titles"][-_HISTORY_CAP:]
    state["used_short_ids"] = state["used_short_ids"][-_HISTORY_CAP:]
    state["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()

    if args.dry_run:
        log.info("[dry-run] would retire %d topic(s) and %d Short item(s).",
                 doc_added, short_added)
        return 0

    Path(STATE_FILE).write_text(json.dumps(state, indent=2), encoding="utf-8")
    log.info("Rotation updated: +%d topic(s), +%d Short item(s).",
             doc_added, short_added)
    _warn_on_runway(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
