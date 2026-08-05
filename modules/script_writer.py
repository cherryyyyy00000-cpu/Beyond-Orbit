"""Turns a topic brief into a documentary script with a 7-layer hook structure.

Retention is the entire game for long-form. A 12-minute video only earns watch
hours if people actually stay, so every script produced here is built around
seven deliberate hook layers:

    1. Thumbnail hook  .... handled in modules/thumbnail.py
    2. Title hook  ........ handled in modules/metadata.py
    3. Cold open (0-3s) ... the topic's `hook` line, spoken FIRST. No logo, no
                            channel intro, no "hey guys" — those cost you the
                            viewers who had not decided yet.
    4. Promise (~12s) ..... what the viewer gets by staying to the end.
    5. Open loops ......... an unresolved question planted between beats, roughly
                            every `script.open_loop_every_seconds`.
    6. Pattern interrupts . visual, not textual — see modules/video_builder.py.
    7. End hook ........... payoff + next-video tease + a reason to subscribe.

Two generation modes
--------------------
* **Gemini** (if ``GEMINI_API_KEY`` is set): the brief is handed over as a
  commission and a full 1500-2200 word script comes back. This is the intended
  mode and hits the ~12 minute target.
* **Templates** (always available): the beats are expanded with connective
  narration locally. Fully offline and dependency-free, but shorter — expect
  roughly 5-7 minutes. Still genuine long-form that counts toward watch hours,
  just less of it per upload.

Public API
----------
    script  = write_script(topic)
    chapters = chapter_timings(script, words, total_duration)
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from modules.config import cfg, get_env, setup_logging
from modules.topic_source import Topic

log = setup_logging(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Section:
    """One chapter of the finished script."""

    heading: str
    text: str


@dataclass
class Script:
    topic_id: str
    title: str
    cold_open: str
    promise: str
    sections: List[Section] = field(default_factory=list)
    outro: str = ""
    source: str = "template"          # "gemini" | "template"

    # -- narration assembly -------------------------------------------------
    def blocks(self) -> List[Tuple[str, str]]:
        """Ordered (chapter_label, text) pairs, in the order they are spoken.

        The cold open and promise are merged into a single opening block because
        YouTube requires the first chapter to start at 00:00 — splitting them
        would create a chapter shorter than the 10-second minimum.
        """
        out: List[Tuple[str, str]] = []
        opening = " ".join(p for p in (self.cold_open, self.promise) if p).strip()
        if opening:
            out.append(("Introduction", opening))
        for s in self.sections:
            if s.text.strip():
                out.append((s.heading, s.text.strip()))
        if self.outro.strip():
            out.append(("What it means", self.outro.strip()))
        return out

    @property
    def full_text(self) -> str:
        """The complete narration handed to edge-tts."""
        return "\n\n".join(text for _, text in self.blocks())

    @property
    def word_count(self) -> int:
        return len(self.full_text.split())

    @property
    def estimated_minutes(self) -> float:
        wpm = float(cfg("script.words_per_minute", 150)) or 150.0
        return self.word_count / wpm

    @property
    def is_publishable(self) -> bool:
        """False if the script is too short to publish as long-form.

        A 2-minute video presented as a documentary tanks average view duration
        and teaches the algorithm the channel is low-value, which is worse than
        not uploading at all. ``generate.py`` checks this and skips the topic
        (without burning it from the rotation) rather than shipping a stub.
        Set ``script.allow_short_publish`` to true to override.
        """
        if bool(cfg("script.allow_short_publish", False)):
            return True
        return self.estimated_minutes >= float(cfg("script.min_publishable_minutes", 6))


# ---------------------------------------------------------------------------
# Connective tissue for the offline template mode
# ---------------------------------------------------------------------------
# Open loops: planted at the END of a beat to pull the viewer into the next one.
_OPEN_LOOPS = [
    "But that raises a problem nobody expected.",
    "And that is where the story stops making sense.",
    "Which leads to the part that is genuinely hard to explain.",
    "That answer, though, creates a much bigger question.",
    "Except the numbers do not quite work out.",
    "And this is where it gets strange.",
    "Hold onto that, because it matters in a moment.",
    "What comes next is the part most people get wrong.",
    "The next detail is the one that changes everything.",
    "And there is a catch.",
]

# Transitions are POSITIONAL, not random. An earlier version sampled these
# randomly and produced mismatches like opening a beat about light bending with
# "Now consider the scale of it." Splitting them by position guarantees the
# connective always makes sense regardless of which beat it lands on.
_TRANSITIONS_FIRST = [
    "Start with the basics.",
    "Here is what we actually know.",
    "Begin with what has been measured.",
]
_TRANSITIONS_MIDDLE = [
    "There is a second piece to this.",
    "Look closer and the picture changes.",
    "This is where the measurements come in.",
    "The evidence comes from an unexpected direction.",
    "Step back for a moment.",
    "Now put those pieces together.",
    "There is more to it than that.",
    "The explanation begins somewhere else.",
]
_TRANSITIONS_LAST = [
    "Which brings us to the part that matters most.",
    "That leaves one last thing to account for.",
    "And that is where this finally comes together.",
]

# Angle-specific framing for the cold open follow-through.
_ANGLE_FRAMING = {
    "what_if": "It sounds like a thought experiment. The physics behind it is completely real.",
    "scale": "Numbers this large stop meaning anything, so we are going to build up to it in steps.",
    "existential": "This is not speculation about the distant future. It is a consequence of what we already measure.",
    "countdown": "Each one is stranger than the last, and the final one is still unexplained.",
    "mystery": "Nobody has a confirmed answer. What we do have is very good data and several bad explanations.",
    "threat": "The risk is real, but it is nothing like the version you have probably heard.",
    "discovery": "This was measured, not guessed — and it did not match what anyone predicted.",
}

_OUTRO_TEMPLATES = [
    "That is where the evidence currently stands. {closing} If you want the next one of these, "
    "subscribing is the only way it reaches you.",
    "So the honest summary is simple: {closing} There is a new deep-space story on this channel "
    "every week, and subscribing is how you catch it.",
    "{closing} Every answer here opened a bigger question, which is the part that makes this "
    "worth following. Subscribe and the next one finds you.",
]

_CLOSINGS = [
    "We are working with real data and incomplete models, and that gap is exactly where the "
    "interesting work happens.",
    "What we know is remarkable. What we do not know is larger, and getting better defined "
    "every year.",
    "None of this required imagination. It required measurement — and the measurements are "
    "stranger than the guesses were.",
    "The universe keeps turning out to be less tidy than our models, and that is a good sign, "
    "not a bad one.",
]


def _rng_for(topic: Topic) -> random.Random:
    """Deterministic-per-topic randomness.

    The same topic always produces the same phrasing, which makes a re-run
    reproducible when you are debugging, while different topics still sound
    different from each other.
    """
    return random.Random(f"beyond-orbit::{topic.id}")


# ---------------------------------------------------------------------------
# Template generation (always available, no network, no key)
# ---------------------------------------------------------------------------
def _template_script(topic: Topic) -> Script:
    """Expand the topic's beats into narration locally."""
    rng = _rng_for(topic)

    cold_open = topic.hook.strip() or f"Here is something about {topic.title.lower()} that does not add up."
    framing = _ANGLE_FRAMING.get(topic.angle, _ANGLE_FRAMING["discovery"])
    promise = " ".join(p for p in (framing, topic.promise.strip()) if p)

    n = len(topic.beats)
    middles = rng.sample(_TRANSITIONS_MIDDLE,
                         k=min(len(_TRANSITIONS_MIDDLE), max(0, n - 2)))
    loops = rng.sample(_OPEN_LOOPS, k=min(len(_OPEN_LOOPS), max(0, n - 1)))

    sections: List[Section] = []
    for i, beat in enumerate(topic.beats):
        parts: List[str] = []
        # Positional transition so the connective always fits the beat's slot.
        if i == 0:
            parts.append(rng.choice(_TRANSITIONS_FIRST))
        elif i == n - 1 and n > 2:
            parts.append(rng.choice(_TRANSITIONS_LAST))
        elif i - 1 < len(middles):
            parts.append(middles[i - 1])

        parts.extend(beat.points)
        # Plant an open loop at the end of every beat except the last one.
        if i < n - 1 and i < len(loops):
            parts.append(loops[i])
        sections.append(Section(heading=beat.heading, text=" ".join(parts)))

    outro = rng.choice(_OUTRO_TEMPLATES).format(closing=rng.choice(_CLOSINGS))
    cta = str(cfg("channel.cta", "")).strip()
    if cta:
        outro = f"{outro} {cta}"

    return Script(
        topic_id=topic.id,
        title=topic.title,
        cold_open=cold_open,
        promise=promise,
        sections=sections,
        outro=outro,
        source="template",
    )


# ---------------------------------------------------------------------------
# Gemini generation (optional — better, longer scripts)
# ---------------------------------------------------------------------------
def _gemini_prompt(topic: Topic) -> str:
    min_w = int(cfg("script.min_words", 1500))
    max_w = int(cfg("script.max_words", 2200))
    loop_s = int(cfg("script.open_loop_every_seconds", 55))
    beats_json = json.dumps(
        [{"heading": b.heading, "points": b.points} for b in topic.beats],
        indent=2,
    )
    channel = str(cfg("channel.name", "Beyond Orbit"))
    niche = str(cfg("channel.niche", "")).strip()
    audience = str(cfg("channel.audience", "")).strip()
    language = str(cfg("channel.language", "English (US)")).strip()

    brief = f'You are the head writer for "{channel}", a faceless YouTube channel.'
    if niche:
        brief += f"\nCHANNEL: {niche}"
    if audience:
        brief += f"\nAUDIENCE: {audience}"
    brief += f"\nLANGUAGE: {language}"

    return f"""{brief}

Write the narration for one video.

TOPIC: {topic.title}
ANGLE: {topic.angle}
COLD OPEN (use this idea as the very first line, you may sharpen the wording): {topic.hook}
PROMISE TO THE VIEWER: {topic.promise}

RESEARCH BEATS (cover all of them, in this order, one script section each):
{beats_json}

HARD REQUIREMENTS
- Total narration between {min_w} and {max_w} words. This is the most important
  constraint. Write full, flowing paragraphs, not bullet points.
- The FIRST sentence must be the cold open. No greeting, no channel name, no
  "welcome back", no "in today's video".
- State the promise within roughly the first 40 words.
- End each section (except the last) with an OPEN LOOP: a short unresolved
  question or tension that makes leaving feel like missing something. Aim for one
  roughly every {loop_s} seconds of speech.
- Spoken register: plain, confident, concrete. Second person where natural.
  Short sentences mixed with longer ones.
- ACCURACY IS NON-NEGOTIABLE. Do not invent numbers, dates, names or missions.
  Where something is uncertain, disputed or hypothetical, SAY SO explicitly
  ("the leading explanation is", "this remains unconfirmed", "estimates range
  from"). A space audience fact-checks in the comments and confident errors cost
  the channel its credibility.
- No markdown, no headings inside the narration text, no stage directions, no
  emoji, no sound-effect cues.
- Do not mention NASA endorsement or imply any partnership.

Return ONLY valid JSON in exactly this shape:
{{
  "cold_open": "the first spoken line",
  "promise": "one or two sentences stating what the viewer gets",
  "sections": [
    {{"heading": "short chapter title, max 5 words", "text": "several paragraphs of narration"}}
  ],
  "outro": "payoff, a tease toward the next video, and a reason to subscribe"
}}"""


# Models that technically advertise generateContent but cannot write a
# documentary script. An earlier version tried EVERY model the API listed, which
# meant burning quota on image, music, TTS and robotics models — 42 attempts per
# run, each with a 20-second retry, taking over seven minutes to fail.
_MODEL_EXCLUDE = (
    "image", "imagen", "nano-banana",          # image generation
    "tts", "audio", "lyria", "veo",            # audio / video generation
    "embedding", "aqa",                        # embeddings / retrieval
    "robotics", "computer-use",                # agentic control
    "deep-research", "antigravity",            # research agents
    "vision",                                  # vision-only variants
)
_MAX_MODELS_TRIED = 4


def _model_candidates(genai) -> List[str]:
    """Text-capable Gemini models to try, best first, capped in number.

    Model names get retired, so the list is discovered from the API rather than
    hardcoded — but it is then filtered to text models and capped, because trying
    everything wastes the free-tier quota that the pipeline depends on.
    """
    forced = get_env("GEMINI_MODEL")
    out: List[str] = [forced] if forced else []

    try:
        available = []
        for m in genai.list_models():
            name = str(getattr(m, "name", "")).replace("models/", "")
            methods = getattr(m, "supported_generation_methods", []) or []
            if not name or "generateContent" not in methods:
                continue
            lowered = name.lower()
            # Only mainline Gemini text models; gemma models return 403 on the
            # free API tier.
            if not lowered.startswith("gemini-"):
                continue
            if any(bad in lowered for bad in _MODEL_EXCLUDE):
                continue
            available.append(name)

        # Prefer flash (fastest, most generous free tier), then pro. Within each
        # group, deprioritise "lite" variants — they are cheaper but noticeably
        # weaker at holding a 1,500-word structure — and prefer unversioned
        # aliases over pinned builds, which get retired.
        def rank(name: str):
            n = name.lower()
            return (
                0 if "flash" in n else 1,       # flash first
                1 if "lite" in n else 0,        # full model before lite
                1 if re.search(r"-\d{3}$", n) else 0,   # alias before pinned build
                1 if "preview" in n or "exp" in n else 0,
                n,
            )

        out += sorted(available, key=rank)
        if available:
            log.info("Gemini text models usable: %d (trying %s)",
                     len(available), ", ".join((out or ["none"])[:_MAX_MODELS_TRIED]))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not list Gemini models (%s) — using a static list.", exc)

    out += ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.0-flash",
            "gemini-2.5-pro"]
    return list(dict.fromkeys([m for m in out if m]))[:_MAX_MODELS_TRIED]


def _gemini_script(topic: Topic) -> Optional[Script]:
    """Ask Gemini for a full script. Returns None on any problem."""
    api_key = get_env("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        import google.generativeai as genai

        genai.configure(api_key=api_key)
    except Exception as exc:  # noqa: BLE001
        log.warning("google-generativeai unavailable (%s) — using templates.", exc)
        return None

    prompt = _gemini_prompt(topic)
    candidates = _model_candidates(genai)
    quota_hits = 0
    fatal: Optional[str] = None

    for model_name in candidates:
        # The free tier limits requests PER MINUTE as well as per day, so a 429
        # is often just "too fast" rather than "out of budget". One backoff retry
        # per model turns a large share of those into successes.
        for attempt in (1, 2):
            try:
                model = genai.GenerativeModel(model_name)
                resp = model.generate_content(
                    prompt,
                    generation_config={
                        "temperature": 0.85,
                        "response_mime_type": "application/json",
                        "max_output_tokens": 8192,
                    },
                )
                raw = (getattr(resp, "text", "") or "").strip()
                if not raw:
                    log.warning("Gemini (%s) returned an empty response.", model_name)
                    break
                parsed = _parse_script_json(raw)
                if parsed:
                    log.info("Script written by Gemini (%s): %d words.",
                             model_name, parsed.word_count)
                    return parsed
                log.warning("Gemini (%s) returned unusable JSON (%d chars).",
                            model_name, len(raw))
                break
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                lowered = msg.lower()

                # PROJECT-level problems affect every model, because quota and
                # access are granted per project, not per model. Trying the rest
                # just burns time and requests — an earlier version spent seven
                # minutes rediscovering the same 429 forty-two times.
                project_blocked = (
                    "quota exceeded for metric" in lowered
                    or "check your plan and billing" in lowered
                    or "denied access" in lowered
                    or "billing" in lowered and "enable" in lowered
                )
                if project_blocked:
                    log.error("Gemini is blocked at the PROJECT level, so no "
                              "other model will work either: %s", msg[:260])
                    fatal = msg
                    break

                # A per-minute rate limit, by contrast, usually clears on retry.
                rate_limited = any(k in lowered for k in
                                   ("429", "rate limit", "resource_exhausted"))
                if rate_limited:
                    quota_hits += 1
                    if attempt == 1:
                        log.info("Gemini %s rate-limited; retrying in 20s...",
                                 model_name)
                        time.sleep(20)
                        continue
                    log.warning("Gemini %s still rate-limited after retry: %s",
                                model_name, msg[:260])
                else:
                    log.warning("Gemini %s failed: %s", model_name, msg[:260])
                break
        if fatal:
            break

    if fatal:
        log.error(
            "Gemini cannot be used with this key. The project is either out of "
            "free-tier quota for the day or restricted.\n"
            "  * check quota and usage:  https://ai.dev/rate-limit\n"
            "  * or create a key in a DIFFERENT project: "
            "https://aistudio.google.com/apikey\n"
            "  * a 'denied access' 403 usually means the project needs billing "
            "enabled (the free tier still applies) or is newly flagged.\n"
            "Optionally set GROQ_API_KEY for a free fallback writer so a Gemini "
            "outage does not stop the channel."
        )
    elif quota_hits:
        log.error("Every candidate Gemini model was rate-limited (%d attempts).",
                  quota_hits)
    return None


_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_MODELS = ("llama-3.3-70b-versatile", "llama-3.1-8b-instant")


def _groq_script(topic: Topic) -> Optional[Script]:
    """Write the script with Groq instead of Gemini.

    A second free provider means one project running out of quota does not stop
    the channel. Groq's API is OpenAI-compatible and needs no extra dependency
    beyond requests, which is already required.
    """
    api_key = get_env("GROQ_API_KEY")
    if not api_key:
        return None
    try:
        import requests
    except ImportError:
        return None

    prompt = _gemini_prompt(topic)
    for model in _GROQ_MODELS:
        try:
            resp = requests.post(
                _GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.85,
                    "max_tokens": 8000,
                    "response_format": {"type": "json_object"},
                },
                timeout=180,
            )
            if resp.status_code != 200:
                log.warning("Groq %s -> HTTP %s: %s", model, resp.status_code,
                            resp.text[:200])
                continue
            raw = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            parsed = _parse_script_json(raw)
            if parsed:
                log.info("Script written by Groq (%s): %d words.",
                         model, parsed.word_count)
                return parsed
            log.warning("Groq %s returned unusable JSON.", model)
        except Exception as exc:  # noqa: BLE001
            log.warning("Groq %s failed: %s", model, str(exc)[:200])
    return None


def _parse_script_json(raw: str) -> Optional[Script]:
    """Parse and validate a model's JSON reply (Gemini or Groq)."""
    text = raw.strip()
    # Strip a ```json fence if the model added one despite the mime type.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Last resort: grab the outermost {...}.
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    if not isinstance(data, dict):
        return None
    rows = data.get("sections")
    if not isinstance(rows, list) or not rows:
        return None

    sections: List[Section] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        heading = str(r.get("heading") or "").strip()
        body = str(r.get("text") or "").strip()
        if body:
            sections.append(Section(heading=heading or "Chapter", text=body))
    if not sections:
        return None

    return Script(
        topic_id="",  # filled in by write_script
        title="",
        cold_open=str(data.get("cold_open") or "").strip(),
        promise=str(data.get("promise") or "").strip(),
        sections=sections,
        outro=str(data.get("outro") or "").strip(),
        source="gemini",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def write_script(topic: Topic) -> Script:
    """Produce a narration script for one topic. Never raises."""
    use_gemini = bool(cfg("topics.use_gemini", True))
    script: Optional[Script] = None

    # Two providers, then templates. One project running out of free quota
    # should not be able to stop the channel on its own.
    if use_gemini:
        script = _gemini_script(topic)
        if script is None:
            script = _groq_script(topic)

    if script is not None:
        # Neither provider knows the ids; graft them on and repair anything left
        # blank using the brief.
        script.topic_id = topic.id
        script.title = topic.title
        if not script.cold_open:
            script.cold_open = topic.hook
        if not script.promise:
            script.promise = topic.promise
    else:
        script = _template_script(topic)
        if use_gemini:
            log.info("Both writers unavailable — using the offline template "
                     "script for %s.", topic.id)

    min_w = int(cfg("script.min_words", 1500))
    target = float(cfg("script.target_minutes", 12))
    floor_min = float(cfg("script.min_publishable_minutes", 6))

    if script.estimated_minutes < floor_min:
        log.warning(
            "Script for %s is only %d words (~%.1f min), under the %.0f-minute "
            "publishable floor. %s",
            topic.id, script.word_count, script.estimated_minutes, floor_min,
            "The offline template mode expands the topic's beats but genuinely "
            "cannot reach documentary length — the beats are a brief, not a "
            "script. Set GEMINI_API_KEY to get full ~12-minute scripts. Padding "
            "this out locally would just be filler, which hurts retention and is "
            "exactly what the inauthentic-content policy targets."
            if script.source == "template" else
            "Gemini returned a short script — worth re-running.",
        )
    elif script.word_count < min_w:
        log.warning("Script for %s is %d words (~%.1f min) — usable, but below "
                    "the %d-word (~%.0f min) target.",
                    topic.id, script.word_count, script.estimated_minutes,
                    min_w, target)
    else:
        log.info("Script for %s: %d words (~%.1f min, source=%s).",
                 topic.id, script.word_count, script.estimated_minutes, script.source)
    return script


# ---------------------------------------------------------------------------
# Chapters
# ---------------------------------------------------------------------------
def chapter_timings(
    script: Script,
    words: Sequence[Dict],
    total_duration: float,
) -> List[Tuple[float, str]]:
    """Map script blocks to timestamps using edge-tts word timings.

    Returns [(seconds, label), ...] with the first entry always at 0.0, or an
    empty list if valid chapters cannot be produced.

    YouTube's rules are strict and silently ignore the whole list if broken:
    the first chapter MUST be at 00:00, there must be at least 3, and each must
    be at least 10 seconds long.
    """
    if not bool(cfg("chapters.enabled", True)):
        return []

    blocks = script.blocks()
    if len(blocks) < int(cfg("chapters.min_count", 3)):
        log.info("Only %d block(s) — not enough for chapters.", len(blocks))
        return []

    # Cumulative word index at which each block starts.
    starts: List[int] = []
    running = 0
    for _, text in blocks:
        starts.append(running)
        running += len(text.split())
    our_total = running

    marks: List[Tuple[float, str]] = []
    if words and our_total:
        # edge-tts emits one WordBoundary per whitespace token, so our count
        # should line up closely. If it drifts badly (different tokenisation of
        # numbers, symbols, etc.) fall back to proportional placement.
        drift = abs(len(words) - our_total) / float(our_total)
        if drift <= 0.15:
            for idx, (label, _) in zip(starts, blocks):
                i = min(max(idx, 0), len(words) - 1)
                marks.append((float(words[i]["start"]), label))
        else:
            log.info("Word-timing drift %.0f%% — placing chapters proportionally.",
                     drift * 100)
            for idx, (label, _) in zip(starts, blocks):
                marks.append((total_duration * (idx / our_total), label))
    else:
        for idx, (label, _) in zip(starts, blocks):
            marks.append((total_duration * (idx / max(1, our_total)), label))

    # Enforce YouTube's rules.
    cleaned: List[Tuple[float, str]] = []
    for t, label in marks:
        t = max(0.0, min(float(t), max(0.0, total_duration - 1.0)))
        if not cleaned:
            cleaned.append((0.0, label))       # first MUST be 00:00
            continue
        if t - cleaned[-1][0] >= 10.0:         # each MUST be >= 10s
            cleaned.append((t, label))

    if len(cleaned) < int(cfg("chapters.min_count", 3)):
        log.info("After enforcing YouTube's 10-second minimum only %d chapter(s) "
                 "remain — omitting chapters entirely.", len(cleaned))
        return []

    log.info("Chapters: %d marks over %.1f min.", len(cleaned), total_duration / 60.0)
    return cleaned


def format_timestamp(seconds: float) -> str:
    """Seconds -> M:SS or H:MM:SS, the format YouTube parses in descriptions."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
