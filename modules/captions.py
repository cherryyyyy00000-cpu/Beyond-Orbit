"""Caption generation — an .srt track for the documentary, karaoke for the Short.

Two deliberately different treatments:

**Long-form (.srt sidecar).** Burning captions into a 12-minute documentary looks
amateurish and covers the visuals you spent the whole render fetching. Instead we
upload a real caption track alongside the video. That is better for
accessibility, better for SEO (YouTube indexes caption text), and the viewer can
switch it off. See ``captions.burn_in`` in config.json.

**Short (.ass karaoke).** Vertical Shorts are watched muted and while scrolling,
so here big burned-in word-by-word captions genuinely lift retention. This is the
one place the FactVault-style treatment is correct.

Public API
----------
    srt = build_srt(words, out_path)
    ass = build_karaoke_ass(words, out_path, width, height, hook=..., cta=...)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from modules.config import cfg, setup_logging

log = setup_logging(__name__)

# Caption line-length limits. Roughly 42 characters per line, two lines max, is
# the long-standing subtitling convention — it stays readable at a glance.
_SRT_MAX_CHARS = 84
_SRT_MAX_SECONDS = 6.0
_SRT_MIN_SECONDS = 0.9


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _clean(word: str) -> str:
    return str(word).replace("\n", " ").strip()


def _group_words(words: Sequence[Dict], per_group: int) -> List[Dict]:
    """Fixed-size word groups with monotonic timings."""
    groups: List[Dict] = []
    for i in range(0, len(words), per_group):
        chunk = list(words[i:i + per_group])
        text = " ".join(_clean(w["word"]) for w in chunk if _clean(w["word"]))
        if not text:
            continue
        groups.append({
            "start": float(chunk[0]["start"]),
            "end": float(chunk[-1]["end"]),
            "text": text,
        })
    for i in range(len(groups) - 1):
        groups[i]["end"] = max(groups[i]["end"], groups[i + 1]["start"])
    return groups


# ---------------------------------------------------------------------------
# .srt — the long-form caption track
# ---------------------------------------------------------------------------
def _srt_time(t: float) -> str:
    """Seconds -> SRT timestamp HH:MM:SS,mmm."""
    t = max(0.0, float(t))
    ms = int(round((t - int(t)) * 1000))
    if ms >= 1000:
        ms = 999
    s = int(t)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d},{ms:03d}"


def _sentence_cues(words: Sequence[Dict]) -> List[Dict]:
    """Build readable cues that break at sentence ends, not fixed word counts.

    A caption track that respects punctuation reads far better than one chopped
    every N words, and YouTube's indexer gets cleaner sentences too.
    """
    cues: List[Dict] = []
    cur: List[Dict] = []

    def flush() -> None:
        if not cur:
            return
        text = " ".join(_clean(w["word"]) for w in cur if _clean(w["word"]))
        if text:
            cues.append({
                "start": float(cur[0]["start"]),
                "end": float(cur[-1]["end"]),
                "text": text,
            })
        cur.clear()

    for w in words:
        cur.append(w)
        token = _clean(w["word"])
        chars = sum(len(_clean(x["word"])) + 1 for x in cur)
        span = float(cur[-1]["end"]) - float(cur[0]["start"])
        ends_sentence = bool(re.search(r"[.!?]\"?$", token))
        if ends_sentence or chars >= _SRT_MAX_CHARS or span >= _SRT_MAX_SECONDS:
            flush()
    flush()

    # Enforce a readable minimum duration without overlapping the next cue.
    for i, c in enumerate(cues):
        if c["end"] - c["start"] < _SRT_MIN_SECONDS:
            limit = cues[i + 1]["start"] if i + 1 < len(cues) else c["start"] + _SRT_MIN_SECONDS
            c["end"] = min(c["start"] + _SRT_MIN_SECONDS, max(limit, c["end"]))
    return cues


def _wrap_two_lines(text: str, max_chars: int = _SRT_MAX_CHARS // 2) -> str:
    """Break a cue into at most two balanced lines."""
    if len(text) <= max_chars:
        return text
    words = text.split()
    mid = len(text) // 2
    best, best_delta = len(words) // 2, None
    run = 0
    for i, w in enumerate(words[:-1]):
        run += len(w) + 1
        delta = abs(run - mid)
        if best_delta is None or delta < best_delta:
            best, best_delta = i + 1, delta
    return "\n".join([" ".join(words[:best]), " ".join(words[best:])])


def build_srt(words: Sequence[Dict], out_path) -> Optional[Path]:
    """Write an .srt caption track from word timings."""
    if not words:
        log.warning("No word timings — skipping the caption track.")
        return None

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cues = _sentence_cues(words)
    if not cues:
        return None

    lines: List[str] = []
    for i, c in enumerate(cues, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_time(c['start'])} --> {_srt_time(c['end'])}")
        lines.append(_wrap_two_lines(c["text"]))
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Caption track written: %s (%d cues)", out_path.name, len(cues))
    return out_path


# ---------------------------------------------------------------------------
# .ass — burned-in karaoke captions for the Short
# ---------------------------------------------------------------------------
def _hex_to_ass(color: str) -> str:
    """#RRGGBB -> ASS &HAABBGGRR (opaque), for Style lines."""
    c = str(color).lstrip("#")
    if len(c) != 6:
        return "&H00FFFFFF"
    r, g, b = c[0:2], c[2:4], c[4:6]
    return f"&H00{b}{g}{r}".upper()


def _ass_inline(color: str) -> str:
    """#RRGGBB -> &HBBGGRR& , for inline \\c overrides."""
    c = str(color).lstrip("#")
    if len(c) != 6:
        return "&HFFFFFF&"
    r, g, b = c[0:2], c[2:4], c[4:6]
    return f"&H{b}{g}{r}&".upper()


def _ass_time(t: float) -> str:
    """Seconds -> ASS timestamp H:MM:SS.cc."""
    t = max(0.0, float(t))
    cs = int(round((t - int(t)) * 100))
    if cs >= 100:
        cs = 99
    s = int(t)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}.{cs:02d}"


def _ass_text(word: str) -> str:
    """Uppercase and strip characters that would break ASS override blocks."""
    return _clean(word).upper().replace("{", "").replace("}", "")


def build_karaoke_ass(
    words: Sequence[Dict],
    out_path,
    width: int,
    height: int,
    hook: Optional[str] = None,
    cta: Optional[str] = None,
    time_offset: float = 0.0,
    total_duration: Optional[float] = None,
) -> Optional[Path]:
    """Write burned-in karaoke captions for the vertical Short.

    Args:
        words: word timings covering the clip.
        width/height: the Short's frame size (1080x1920).
        hook: big attention line shown over roughly the first 2.5 seconds.
        cta: closing line shown at the end.
        time_offset: subtract this from every timing. The Short is cut out of the
            middle of the documentary, so its word timings are on the long-form
            timeline and have to be rebased to zero.
        total_duration: clip length, used to place the CTA.

    Returns the path, or None if disabled or there is nothing to write.
    """
    if not bool(cfg("shorts.captions_enabled", True)):
        return None
    if not words:
        log.warning("No word timings for the Short — skipping karaoke captions.")
        return None

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    font_size = int(cfg("shorts.captions_font_size", 78))
    per_group = max(1, int(cfg("shorts.captions_words_per_group", 3)))
    karaoke = bool(cfg("shorts.captions_highlight_active_word", True))

    primary = _hex_to_ass(str(cfg("captions.primary_color", "#FFFFFF")))
    highlight = _hex_to_ass(str(cfg("captions.highlight_color", "#5C94FF")))
    outline_col = _hex_to_ass(str(cfg("captions.outline_color", "#000000")))
    outline_w = int(cfg("captions.outline_width", 4)) + 2  # thicker for vertical
    hl_inline = _ass_inline(str(cfg("captions.highlight_color", "#5C94FF")))
    pr_inline = _ass_inline(str(cfg("captions.primary_color", "#FFFFFF")))

    pos_x = width // 2
    pos_y = int(height * 0.62)
    hook_y = int(height * 0.30)
    hook_fs = int(font_size * 1.15)
    cta_fs = int(font_size * 0.85)

    # Rebase onto the clip's own timeline and drop anything outside it.
    rebased: List[Dict] = []
    for w in words:
        s = float(w["start"]) - time_offset
        e = float(w["end"]) - time_offset
        if e <= 0:
            continue
        if total_duration is not None and s >= total_duration:
            break
        rebased.append({"start": max(0.0, s), "end": max(0.05, e), "word": w["word"]})
    if not rebased:
        return None

    narration_end = rebased[-1]["end"]
    clip_end = float(total_duration) if total_duration else narration_end

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,DejaVu Sans,{font_size},{primary},{primary},{outline_col},&H64000000,-1,0,0,0,100,100,0,0,1,{outline_w},2,5,40,40,40,1
Style: Hook,DejaVu Sans,{hook_fs},{highlight},{highlight},{outline_col},&H64000000,-1,0,0,0,100,100,0,0,1,{outline_w + 2},3,5,40,40,40,1
Style: CTA,DejaVu Sans,{cta_fs},{highlight},{highlight},{outline_col},&H64000000,-1,0,0,0,100,100,0,0,1,{outline_w},2,5,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines: List[str] = [header]
    cues = 0

    # --- Opening hook -------------------------------------------------------
    if hook:
        hook_end = min(2.6, max(1.6, clip_end - 0.5))
        eff = f"{{\\an5\\pos({pos_x},{hook_y})\\fad(80,150)}}"
        lines.append(
            f"Dialogue: 1,{_ass_time(0.0)},{_ass_time(hook_end)},Hook,,0,0,0,,"
            f"{eff}{_ass_text(hook)}\n"
        )
        cues += 1

    # --- Word-by-word captions ---------------------------------------------
    if karaoke:
        for idx, w in enumerate(rebased):
            grp_i = idx // per_group
            group = rebased[grp_i * per_group: grp_i * per_group + per_group]
            pos_in = idx - grp_i * per_group

            start = w["start"]
            end = rebased[idx + 1]["start"] if idx + 1 < len(rebased) else w["end"] + 0.3
            if end <= start:
                end = start + 0.18

            tokens: List[str] = []
            for j, gw in enumerate(group):
                tok = _ass_text(gw["word"])
                if not tok:
                    continue
                if j == pos_in:
                    # Active word: brand colour plus a small size "pop".
                    tokens.append(
                        f"{{\\fscx112\\fscy112\\c{hl_inline}}}{tok}"
                        f"{{\\fscx100\\fscy100\\c{pr_inline}}}"
                    )
                else:
                    tokens.append(tok)
            text = " ".join(tokens)
            if not text:
                continue
            eff = f"{{\\an5\\pos({pos_x},{pos_y})}}"
            lines.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Cap,,0,0,0,,{eff}{text}\n"
            )
            cues += 1
    else:
        for g in _group_words(rebased, per_group):
            eff = f"{{\\an5\\pos({pos_x},{pos_y})\\fad(60,60)}}"
            lines.append(
                f"Dialogue: 0,{_ass_time(g['start'])},{_ass_time(g['end'])},Cap,,0,0,0,,"
                f"{eff}{_ass_text(g['text'])}\n"
            )
            cues += 1

    # --- Closing CTA --------------------------------------------------------
    if cta:
        # Anchor the CTA to the END of the clip and cap how long it is on screen.
        # Anchoring it to the narration instead would leave it plastered across
        # most of the Short whenever the narration finishes early.
        cta_len = 2.6
        cta_end = max(narration_end, clip_end)
        cta_start = max(0.0, cta_end - cta_len)
        eff = f"{{\\an5\\pos({pos_x},{pos_y})\\fad(120,80)}}"
        lines.append(
            f"Dialogue: 1,{_ass_time(cta_start)},{_ass_time(cta_end)},CTA,,0,0,0,,"
            f"{eff}{_ass_text(cta)}\n"
        )
        cues += 1

    out_path.write_text("".join(lines), encoding="utf-8")
    log.info("Karaoke captions written: %s (%d cues, karaoke=%s)",
             out_path.name, cues, karaoke)
    return out_path
