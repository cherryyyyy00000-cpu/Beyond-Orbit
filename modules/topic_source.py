"""Topic selection for Beyond Orbit.

Reads ``topics/space_bank.json`` and hands out the next unused topic. A topic is
a BRIEF — a hook, a promise, and a set of beats — not a finished script;
``modules/script_writer.py`` turns it into narration.

Rotation is driven by ``rotation_state.json``, and — importantly — a topic is
only marked used AFTER the video successfully uploads (see
``finalize_rotation.py``). A failed upload must never burn a topic from the
queue.

Public API:
    topic = get_next_topic(used_ids={"t001", ...})
    all_  = load_topics()
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from modules.config import TOPICS_DIR, cfg, setup_logging

log = setup_logging(__name__)


@dataclass
class Beat:
    """One chapter's worth of material."""

    heading: str
    points: List[str] = field(default_factory=list)


@dataclass
class Topic:
    """A single video brief."""

    id: str
    title: str
    angle: str = "discovery"
    hook: str = ""
    promise: str = ""
    beats: List[Beat] = field(default_factory=list)
    visual_queries: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    @property
    def beat_count(self) -> int:
        return len(self.beats)


def _bank_path() -> Path:
    name = str(cfg("topics.source", "space_bank.json"))
    return TOPICS_DIR / name


def load_topics() -> List[Topic]:
    """Load and validate the topic bank. Returns [] if it cannot be read."""
    path = _bank_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.error("Topic bank not found: %s", path)
        return []
    except json.JSONDecodeError as exc:
        log.error("Topic bank is invalid JSON (%s): %s", path, exc)
        return []

    rows = raw.get("topics") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        log.error("Topic bank has no 'topics' list.")
        return []

    topics: List[Topic] = []
    seen: Set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("id") or "").strip()
        title = str(row.get("title") or "").strip()
        if not tid or not title:
            log.warning("Skipping topic with missing id/title: %r", row.get("id"))
            continue
        if tid in seen:
            log.warning("Duplicate topic id %r — keeping the first.", tid)
            continue
        seen.add(tid)

        beats: List[Beat] = []
        for b in row.get("beats") or []:
            if not isinstance(b, dict):
                continue
            heading = str(b.get("heading") or "").strip()
            points = [str(p).strip() for p in (b.get("points") or []) if str(p).strip()]
            if heading and points:
                beats.append(Beat(heading=heading, points=points))

        if not beats:
            log.warning("Topic %s has no usable beats — skipping.", tid)
            continue

        topics.append(
            Topic(
                id=tid,
                title=title,
                angle=str(row.get("angle") or "discovery"),
                hook=str(row.get("hook") or "").strip(),
                promise=str(row.get("promise") or "").strip(),
                beats=beats,
                visual_queries=[str(q) for q in (row.get("visual_queries") or []) if str(q).strip()],
                tags=[str(t) for t in (row.get("tags") or []) if str(t).strip()],
            )
        )

    log.info("Topic bank loaded: %d usable topic(s) from %s", len(topics), path.name)
    return topics


def get_next_topic(used_ids: Optional[Iterable[str]] = None) -> Optional[Topic]:
    """Return the next unused topic, or None if the bank is empty.

    ``topics.order`` controls behaviour:
      "sequence" (default) walks the bank in order — predictable and easy to
        reason about when you are checking which videos have gone out.
      "random" picks a random unused topic.

    If EVERY topic has been used, we log loudly and return None rather than
    silently looping. Re-posting the same 50 topics is exactly what YouTube's
    inauthentic-content policy targets, so this must be a visible decision:
    either add topics to the bank, or clear rotation_state.json on purpose.
    """
    used = {str(u) for u in (used_ids or set())}
    topics = load_topics()
    if not topics:
        return None

    fresh = [t for t in topics if t.id not in used]
    if not fresh:
        log.error(
            "Every one of the %d topics in the bank has been used. Add new topics "
            "to topics/%s rather than letting the channel repeat itself — "
            "repeated/mass-produced content is what YouTube's inauthentic content "
            "policy penalises.",
            len(topics), _bank_path().name,
        )
        return None

    order = str(cfg("topics.order", "sequence")).lower()
    pick = random.choice(fresh) if order == "random" else fresh[0]

    log.info("Topic [%s] %r (angle=%s, %d beats, %d/%d remaining)",
             pick.id, pick.title, pick.angle, pick.beat_count, len(fresh), len(topics))
    return pick


def get_topic_by_id(topic_id: str) -> Optional[Topic]:
    """Fetch one specific topic — handy for testing a single video."""
    for t in load_topics():
        if t.id == topic_id:
            return t
    log.error("Topic id %r not found in the bank.", topic_id)
    return None
