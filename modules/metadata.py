"""Titles, descriptions, chapters, hashtags and the attribution block.

Everything YouTube reads about a video, assembled in one place:

* **title** — keyword first (so search matches), curiosity second (so humans
  click). Capped at 100 characters, which is YouTube's hard limit.
* **description** — the first two lines are all most viewers ever see, so the
  hook goes there. Then chapters, then the CTA, then attribution, then hashtags.
* **chapters** — timestamps from ``script_writer.chapter_timings``. These both
  raise watch time (viewers re-watch sections) and add keyword surface.
* **hashtags** — only the FIRST THREE render above the title, so those three are
  chosen deliberately rather than dumped from a pool.
* **attribution** — NASA does not require credit, but ESA/Hubble and ESO
  material is CC BY 4.0, where credit is a licence condition. Crediting
  everything is simpler and safer than tracking which asset needs it.

Gemini sharpens the title and hook when ``GEMINI_API_KEY`` is set; strong offline
templates keep the pipeline fully self-sufficient when it is not.

Public API
----------
    meta = build_metadata(topic, script, chapters, assets, used_titles=set())
    meta = build_short_metadata(topic, parent_title)
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from modules.config import cfg, get_env, setup_logging
from modules.script_writer import Script, format_timestamp

log = setup_logging(__name__)


@dataclass
class VideoMeta:
    title: str
    description: str
    tags: List[str] = field(default_factory=list)
    thumbnail_hook: str = ""
    source: str = "template"


# ---------------------------------------------------------------------------
# Title construction
# ---------------------------------------------------------------------------
# Curiosity-gap patterns that do NOT lie about the content. Clickbait that
# misrepresents the video gets punished by retention, which is the metric that
# actually decides whether the channel grows.
_TITLE_PATTERNS = [
    "{title}",
    "{title} | Beyond Orbit",
    "{title} — What We Actually Know",
    "{title} (Explained)",
]

_ANGLE_TITLE_HINTS = {
    "what_if": ["{title}", "{title} — Step By Step"],
    "scale": ["{title}", "{title} | Size Comparison"],
    "existential": ["{title}", "{title} — The Real Timeline"],
    "countdown": ["{title}", "{title} (Ranked)"],
    "mystery": ["{title}", "{title} — Still Unexplained"],
    "threat": ["{title}", "{title} — How Real Is The Risk?"],
    "discovery": ["{title}", "{title} — What Changed"],
}

_HOOK_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "at", "to", "is", "are", "was", "were",
    "and", "or", "but", "that", "this", "it", "its", "be", "been", "has", "have",
    "will", "would", "could", "should", "not", "no", "so", "if", "as", "by",
    "for", "with", "from", "your", "you", "our", "we",
}

# A thumbnail line that stops on one of these reads as a truncation bug.
_BAD_ENDINGS = _HOOK_STOPWORDS | {
    "than", "when", "where", "what", "how", "why", "into", "onto", "about",
    "refuse", "refuses", "made", "goes", "being", "makes", "came", "does",
    "like", "such", "just", "even", "only", "very", "more", "most", "actually",
    "looks", "look", "seen", "seem", "seems", "after", "before", "over",
}

# ...and one that STARTS on one of these dangles.
_BAD_OPENINGS = {
    "and", "or", "but", "of", "to", "that", "than", "as", "so", "if", "by",
    "with", "from", "for", "is", "are", "was", "were", "be", "been",
    "has", "had", "have", "which", "who", "whose", "its", "their", "it",
    "also", "then", "there", "here",
    "like", "such", "actually", "looks", "look", "just", "even", "only", "very",
}


def _thumbnail_hook(title: str, max_words: int) -> str:
    """Short, punchy, uppercase line for the thumbnail.

    On a phone a thumbnail is roughly 120 pixels wide, so more than a handful of
    words is unreadable — and unreadable means unclicked.

    The window is kept CONTIGUOUS rather than filtering out function words.
    Dropping them turns "What If You Fell Into a Black Hole" into
    "WHAT FELL INTO BLACK HOLE", which is broken English; taking the densest
    run of words instead gives "FELL INTO A BLACK HOLE", which still reads.
    """
    # Keep separators INSIDE numbers. Stripping all punctuation turns
    # "100,000 light years" into "100 000 LIGHT YEARS" and "13.8 billion" into
    # "13 8 BILLION", which reads as corrupted text on a thumbnail.
    #
    # Order matters: allow . and , through the first pass, THEN drop the ones
    # that are not sitting between two digits. (Using a placeholder character
    # does not work here — any placeholder that survives \w would itself be
    # stripped by the same character class.)
    clean = re.sub(r"[^\w\s'\-.,]", " ", str(title))
    clean = re.sub(r"(?<!\d)[.,]|[.,](?!\d)", " ", clean).strip()

    words = [w for w in clean.split() if w]
    if not words:
        return ""
    if len(words) <= max_words:
        return " ".join(words).upper()

    # Score every contiguous window and keep the one richest in content words,
    # then penalise windows that begin or end mid-phrase. Without the ending
    # penalty you get lines like "WHAT WEBB FOUND SHOULD NOT", which stop on a
    # cliff and read as an error rather than a hook.
    best_start, best_score = 0, -99.0
    for start in range(0, len(words) - max_words + 1):
        window = words[start:start + max_words]
        score = sum(1.0 for w in window if w.lower() not in _HOOK_STOPWORDS)
        score += start * 0.01  # slight nudge toward the payoff at the end
        if window[-1].lower() in _BAD_ENDINGS:
            score -= 2.5
        if window[0].lower() in _BAD_OPENINGS:
            score -= 1.5
        if score > best_score:
            best_start, best_score = start, score

    return " ".join(words[best_start:best_start + max_words]).upper()


def _pick_title(topic, used_titles: Optional[set]) -> str:
    used = used_titles or set()
    patterns = _ANGLE_TITLE_HINTS.get(topic.angle, _TITLE_PATTERNS)
    rng = random.Random(f"beyond-orbit-title::{topic.id}")
    options = [p.format(title=topic.title) for p in patterns]
    fresh = [o for o in options if o not in used] or options
    return rng.choice(fresh)[:100]


# ---------------------------------------------------------------------------
# Tags + hashtags
# ---------------------------------------------------------------------------
def _build_tags(topic, limit: int = 28) -> List[str]:
    """Merge topic-specific tags with the channel defaults."""
    out: List[str] = []
    seen = set()
    for raw in list(topic.tags) + list(cfg("youtube.default_tags", []) or []):
        # YouTube counts a tag with spaces oddly and rejects some; keep them
        # short and trimmed.
        t = str(raw).strip().lower()
        if t and t not in seen and len(t) <= 40:
            seen.add(t)
            out.append(t)
    return out[:limit]


def _title_hashtags() -> List[str]:
    """The three hashtags that appear ABOVE the title.

    YouTube renders only the first three from the description, so this is a
    deliberate choice, not a dump.
    """
    tags = [str(h).strip() for h in (cfg("youtube.title_hashtags", []) or [])]
    tags = [t if t.startswith("#") else f"#{t}" for t in tags if t]
    return tags[:3]


# ---------------------------------------------------------------------------
# Description assembly
# ---------------------------------------------------------------------------
def _chapter_block(chapters: Sequence[Tuple[float, str]]) -> str:
    if not chapters:
        return ""
    lines = ["Chapters"]
    for seconds, label in chapters:
        lines.append(f"{format_timestamp(seconds)} {label}")
    return "\n".join(lines)


def _attribution_block(assets: Sequence) -> str:
    """Credit lines for the imagery used."""
    try:
        from modules.nasa_fetch import attribution_lines
        credits = attribution_lines(assets)
    except Exception:  # noqa: BLE001
        credits = []
    if not credits:
        return ""
    lines = [
        "Imagery credit",
        *(f"- {c}" for c in credits),
        "",
        "NASA content is generally not subject to copyright in the United "
        "States. ESA/Hubble and ESO material is used under CC BY 4.0. This "
        "channel is not affiliated with, endorsed by, or sponsored by NASA or "
        "ESA.",
    ]
    return "\n".join(lines)


def _build_description(
    topic,
    script: Script,
    chapters: Sequence[Tuple[float, str]],
    assets: Sequence,
    opening_hook: str,
) -> str:
    """Assemble the full description, front-loading the hook."""
    cta = str(cfg("channel.cta", "")).strip()
    tagline = str(cfg("channel.tagline", "")).strip()

    parts: List[str] = []

    # The first two lines are the only ones shown before "...more".
    parts.append(opening_hook.strip())
    if script.promise:
        parts.append(script.promise.strip())

    chapter_text = _chapter_block(chapters)
    if chapter_text:
        parts.append(chapter_text)

    if cta:
        parts.append(cta)

    name = str(cfg("channel.name", "Beyond Orbit")).strip()
    handle = str(cfg("channel.handle", "")).strip()
    about = f"{name} — {tagline}" if tagline else name
    if handle:
        about = f"{about}\n{handle}"
    niche = str(cfg("channel.niche", "")).strip()
    blurb = (niche if niche else
             "Deep-space documentaries built from real mission imagery.")
    contact = str(cfg("channel.contact", "")).strip()
    about = f"{about}\n{blurb}"
    if contact and contact.lower() != str(cfg("channel.name", "")).strip().lower():
        about = f"{about}\n{contact}"
    parts.append(about)

    attribution = _attribution_block(assets)
    if attribution:
        parts.append(attribution)

    hashtags = _title_hashtags()
    extra = [f"#{t.replace(' ', '')}" for t in list(topic.tags)[:6]]
    all_tags = list(dict.fromkeys(hashtags + extra))
    if all_tags:
        parts.append(" ".join(all_tags))

    return "\n\n".join(p for p in parts if p).strip()[:4900]


# ---------------------------------------------------------------------------
# Gemini polish (optional)
# ---------------------------------------------------------------------------
def _gemini_title_and_hook(topic, script: Script) -> Optional[Tuple[str, str, str]]:
    """Return (title, description_hook, thumbnail_hook) or None."""
    api_key = get_env("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        max_words = int(cfg("thumbnail.max_words", 5))
        prompt = (
            "You write titles for a faceless YouTube SPACE DOCUMENTARY channel "
            "aimed at a US audience. This is long-form (10-15 minutes), not "
            "Shorts.\n\n"
            f"Working title: {topic.title}\n"
            f"Angle: {topic.angle}\n"
            f"Opening line of the video: {script.cold_open}\n\n"
            "Return EXACTLY three lines, nothing else:\n"
            "Line 1: the video TITLE. Under 70 characters. Put the searchable "
            "keyword FIRST, then the curiosity. No emoji. No ALL CAPS words. It "
            "must not promise anything the video does not deliver.\n"
            "Line 2: a one-sentence description hook, under 150 characters, that "
            "makes someone want to press play.\n"
            f"Line 3: a THUMBNAIL line of at most {max_words} words, UPPERCASE, "
            "no punctuation. It must be readable at thumbnail size.\n"
            "No labels, no quotes, no markdown."
        )
        env_model = get_env("GEMINI_MODEL", "gemini-2.0-flash")
        for name in dict.fromkeys([env_model, "gemini-2.0-flash", "gemini-flash-latest"]):
            try:
                model = genai.GenerativeModel(name)
                resp = model.generate_content(prompt, generation_config={"temperature": 0.95})
                raw = (getattr(resp, "text", "") or "").strip()
                lines = [l.strip(" \"'`*-") for l in raw.splitlines() if l.strip()]
                if len(lines) >= 3:
                    return lines[0][:100], lines[1][:300], lines[2][:60].upper()
            except Exception as exc:  # noqa: BLE001
                if any(k in str(exc).lower() for k in ("429", "quota", "rate limit")):
                    log.info("Gemini quota exhausted — using template metadata.")
                    return None
                log.warning("Gemini model %s failed (%s).", name, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("Gemini unavailable for metadata (%s).", exc)
        return None


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def build_metadata(
    topic,
    script: Script,
    chapters: Optional[Sequence[Tuple[float, str]]] = None,
    assets: Optional[Sequence] = None,
    used_titles: Optional[set] = None,
) -> VideoMeta:
    """Build complete metadata for one long-form documentary."""
    chapters = list(chapters or [])
    assets = list(assets or [])
    max_words = int(cfg("thumbnail.max_words", 5))

    title = _pick_title(topic, used_titles)
    desc_hook = script.cold_open or topic.hook
    thumb_hook = _thumbnail_hook(topic.title, max_words)
    source = "template"

    polished = _gemini_title_and_hook(topic, script)
    if polished:
        g_title, g_hook, g_thumb = polished
        title = g_title or title
        desc_hook = g_hook or desc_hook
        # Re-run our own shortener over Gemini's line so the word cap is always
        # respected even if the model ignored it.
        thumb_hook = _thumbnail_hook(g_thumb, max_words) or thumb_hook
        source = "gemini"

    description = _build_description(topic, script, chapters, assets, desc_hook)
    tags = _build_tags(topic)

    log.info("Metadata [%s] title=%r thumb=%r tags=%d chapters=%d",
             source, title, thumb_hook, len(tags), len(chapters))
    return VideoMeta(title=title, description=description, tags=tags,
                     thumbnail_hook=thumb_hook, source=source)


def _topic_keyword(topic, words: int = 4) -> str:
    """A short searchable core of the topic title, e.g. 'Black Hole'."""
    return _thumbnail_hook(topic.title, words).title()


def build_short_metadata(topic, parent_title: str = "",
                         parent_video_id: Optional[str] = None,
                         heading: Optional[str] = None,
                         hook: Optional[str] = None) -> VideoMeta:
    """Metadata for one vertical Short.

    The title and description are built from the specific BEAT, not the topic.
    Deriving them from the topic alone gave every beat of a topic an identical
    title and description — three Shorts from one documentary all shipped as
    "Largest Object In The Universe Makes No Sense", which reads as duplicate
    content to both viewers and YouTube even though the videos differ.

    Args:
        topic: the parent Topic (for keywords, tags and the CTA).
        parent_title: fallback text if no beat hook is supplied.
        parent_video_id: links the Short back to the full documentary.
        heading: the beat's heading — the main source of a unique title.
        hook: the beat's strongest line — used as the description opener.
    """
    suffix = str(cfg("shorts.title_suffix", "#shorts")).strip()
    keyword = _topic_keyword(topic)

    # Countdown numbering ("2 - Something pulling our galaxy") is meaningless
    # once a beat stands alone.
    core = re.sub(r"^\s*\d+\s*[-\u2013\u2014:.]\s*", "", str(heading or "")).strip()
    core = re.sub(r"\s*[-\u2013\u2014]\s*", ": ", core)

    if core and len(core.split()) >= 3:
        # Descriptive enough to lead with; add the topic keyword for search
        # unless it is already implied.
        base = core.title()
        if keyword and keyword.lower() not in base.lower():
            base = f"{base} | {keyword}"
    elif core:
        # Too terse on its own (e.g. "Spaghettification") — lead with the topic.
        base = f"{keyword}: {core.title()}" if keyword else core.title()
    else:
        base = keyword or "Beyond Orbit"

    title = f"{base} {suffix}".strip()[:100]

    lines = [hook or topic.hook or parent_title]
    if parent_video_id:
        lines.append(f"Full documentary: https://youtu.be/{parent_video_id}")
    else:
        lines.append("Full documentary on the channel.")
    cta = str(cfg("channel.cta", "")).strip()
    if cta:
        lines.append(cta)

    hashtags = list(dict.fromkeys(
        _title_hashtags() + [f"#{t.replace(' ', '')}" for t in list(topic.tags)[:5]]
        + ["#shorts"]
    ))
    lines.append(" ".join(hashtags))

    return VideoMeta(
        title=title,
        description="\n\n".join(lines)[:4900],
        tags=_build_tags(topic),
        thumbnail_hook=_thumbnail_hook(topic.title, int(cfg("thumbnail.max_words", 5))),
        source="template",
    )
