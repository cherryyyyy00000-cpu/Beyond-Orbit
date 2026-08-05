"""Standalone Short items, derived from the topic bank's beats.

Why derive instead of writing a separate bank
---------------------------------------------
Three Shorts a day is 21 a week. Hand-authoring a bank that big — and then
refilling it every five weeks — is not sustainable.

But the content already exists. Each of the 50 topics carries 4-7 beats, and each
beat is a self-contained idea with 2-3 concrete supporting lines. The bank holds
253 beats, so it yields roughly **249 usable Shorts** — about twelve weeks at
three a day — and it grows by 4-7 more every time a topic is added.

At 150 words per minute a beat plus its framing runs **16-28 seconds**, not 40.
That is the right length: Shorts are ranked on completion rate, and a 20-second
clip gets finished far more often than a 40-second one.

It also creates a natural funnel: a Short is one chapter of a documentary that
exists on the channel, so "full story on the channel" is a real promise rather
than a bait line.

Rotation is tracked separately from documentaries (``used_short_ids`` vs
``used_topic_ids``), so publishing a Short never consumes a documentary topic —
the same material legitimately appears in both formats.

Public API
----------
    items  = load_short_items()
    picks  = get_next_shorts(count=3, used_ids={...})
    script = build_short_script(item)
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set

from modules.config import cfg, setup_logging
from modules.topic_source import Topic, load_topics

log = setup_logging(__name__)

# Words that mark a concrete, surprising claim. Used to decide which line of a
# beat should open the Short — specificity holds a scrolling viewer, vagueness
# does not.
_HOOK_WORDS = {
    "billion", "million", "trillion", "thousand", "hundred", "percent",
    "never", "nobody", "impossible", "cannot", "actually", "instantly",
    "faster", "larger", "bigger", "smaller", "hotter", "colder", "older",
    "strange", "wrong", "unexplained", "unknown", "surprising", "forever",
    "degrees", "kilometres", "kilometers", "miles", "times", "years",
    "seconds", "everything", "nothing", "every",
}

# Length bounds, set from the real distribution in the bank: 253 beats, 21-53
# words, median 33. With ~16 words of framing added, that lands most Shorts at
# 16-28 seconds.
#
# That is DELIBERATE, not a shortfall. Shorts are ranked on completion rate, and
# a 20-second clip is finished far more often than a 40-second one. An earlier
# minimum of 45 words chased a 40-second target and cut the usable pool from 249
# items to 8 — much worse content for a worse format.
_MIN_WORDS = 25
_MAX_WORDS = 130

_OPENERS = [
    "Here is something that sounds wrong.",
    "This one is hard to believe.",
    "Most people get this backwards.",
    "This should not be possible.",
    "Nobody tells you this part.",
    "This is the part that breaks people.",
]

_CTAS = [
    "The full breakdown is on the channel.",
    "There is a full documentary on this on the channel.",
    "Full story on the channel.",
]


@dataclass
class ShortItem:
    """One beat of one topic, packaged as a standalone Short."""

    id: str                      # e.g. "t001#b2"
    topic_id: str
    topic_title: str
    angle: str
    beat_index: int
    heading: str
    points: List[str] = field(default_factory=list)
    visual_queries: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return sum(len(p.split()) for p in self.points)


def _score_line(line: str) -> float:
    """How well a line works as the opening hook. Higher is better."""
    score = 0.0
    for raw in line.split():
        t = raw.lower().strip(".,!?\"'()")
        if t in _HOOK_WORDS:
            score += 2.0
        if any(c.isdigit() for c in t):
            score += 1.5
    if "?" in line:
        score += 1.5
    # Very long sentences make poor hooks — a Short has about three seconds.
    words = len(line.split())
    if words > 28:
        score -= 2.0
    elif words <= 16:
        score += 1.0
    return score


def load_short_items() -> List[ShortItem]:
    """Expand every topic beat into a Short item."""
    items: List[ShortItem] = []
    for topic in load_topics():
        for i, beat in enumerate(topic.beats):
            words = sum(len(p.split()) for p in beat.points)
            if words < _MIN_WORDS:
                # Too thin to carry 40 seconds on its own.
                continue
            items.append(
                ShortItem(
                    id=f"{topic.id}#b{i}",
                    topic_id=topic.id,
                    topic_title=topic.title,
                    angle=topic.angle,
                    beat_index=i,
                    heading=beat.heading,
                    points=list(beat.points),
                    visual_queries=list(topic.visual_queries),
                    tags=list(topic.tags),
                )
            )
    log.info("Short pool: %d item(s) derived from the topic bank.", len(items))
    return items


def get_next_shorts(count: int = 3,
                    used_ids: Optional[Iterable[str]] = None) -> List[ShortItem]:
    """Return up to ``count`` unused Short items.

    Consecutive picks are spread across DIFFERENT topics where possible. Posting
    three beats of the same documentary back to back on the same day would look
    like the same video three times.
    """
    used: Set[str] = {str(u) for u in (used_ids or set())}
    pool = [it for it in load_short_items() if it.id not in used]
    if not pool:
        log.error(
            "Every Short item in the pool has been used. Add topics to "
            "topics/space_bank.json — each new topic adds 4-7 more Shorts. "
            "Recycling the same clips is what the inauthentic-content policy "
            "penalises."
        )
        return []

    order = str(cfg("topics.order", "sequence")).lower()
    if order == "random":
        random.shuffle(pool)

    picks: List[ShortItem] = []
    seen_topics: Set[str] = set()
    # First pass: one beat per topic.
    for item in pool:
        if len(picks) >= count:
            break
        if item.topic_id in seen_topics:
            continue
        picks.append(item)
        seen_topics.add(item.topic_id)
    # Second pass: if the pool is nearly exhausted, allow repeats of a topic.
    if len(picks) < count:
        for item in pool:
            if len(picks) >= count:
                break
            if item not in picks:
                picks.append(item)

    for p in picks:
        log.info("Short [%s] %r <- %s (%d words)",
                 p.id, p.heading, p.topic_id, p.word_count)
    if len(picks) < count:
        log.warning("Only %d Short item(s) available (wanted %d).",
                    len(picks), count)
    return picks


def build_short_script(item: ShortItem) -> Dict[str, str]:
    """Assemble the narration for one Short.

    Returns ``{"text": ..., "hook": ..., "caption_hook": ...}``.

    Structure is hook-first by design: the strongest line of the beat is promoted
    to the front, the rest becomes the payoff, and the CTA closes. A Short has no
    room for a preamble — the first sentence either earns the next three seconds
    or it does not.
    """
    rng = random.Random(f"beyond-orbit-short::{item.id}")

    lines = [p.strip() for p in item.points if p.strip()]
    if not lines:
        return {"text": "", "hook": "", "caption_hook": ""}

    # Promote the most striking line to the front.
    best = max(range(len(lines)), key=lambda i: _score_line(lines[i]))
    hook_line = lines[best]
    rest = lines[:best] + lines[best + 1:]

    parts: List[str] = [hook_line]
    if rest:
        parts.append(rng.choice(_OPENERS))
        parts.extend(rest)
    parts.append(rng.choice(_CTAS))

    text = " ".join(parts)

    # Trim if a beat is unusually long — going past ~60s stops it being a Short.
    words = text.split()
    if len(words) > _MAX_WORDS:
        text = " ".join(words[:_MAX_WORDS])
        if not text.rstrip().endswith((".", "!", "?")):
            text = text.rstrip(",;: ") + "."

    # Big on-screen line for the first ~2.5 seconds. Keep it short: it has to be
    # readable at a glance on a phone.
    caption_hook = _caption_hook(item.heading, hook_line)

    return {"text": text, "hook": hook_line, "caption_hook": caption_hook}


def _caption_hook(heading: str, hook_line: str, max_words: int = 5) -> str:
    """A punchy uppercase overlay line for the opening of the Short.

    Reuses the thumbnail shortener from modules.metadata, which picks the densest
    CONTIGUOUS run of words and penalises windows that start or end mid-phrase.
    Naively taking the first five words produced lines like
    "THE MILKY WAY IS ABOUT" and "THE UNIVERSE IS ABOUT 13", which read as a
    truncation bug rather than a hook.
    """
    # Countdown headings in the bank look like "2 - Something pulling our galaxy";
    # the list numbering is meaningless once the beat stands alone.
    source = re.sub(r"^\s*\d+\s*[-–—:.]\s*", "", str(heading or "")).strip()
    if not source or len(source.split()) > max_words:
        source = hook_line or source
    if not source:
        return "BEYOND ORBIT"

    try:
        from modules.metadata import _thumbnail_hook
        out = _thumbnail_hook(source, max_words)
        if out:
            return out
    except Exception:  # noqa: BLE001
        pass

    clean = re.sub(r"[^\w\s'-]", " ", source).strip()
    words = [w for w in clean.split() if w]
    return " ".join(words[:max_words]).upper() or "BEYOND ORBIT"
