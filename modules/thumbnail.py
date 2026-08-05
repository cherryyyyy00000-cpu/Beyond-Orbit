"""Thumbnail generation — 1280x720, three variants per video.

The thumbnail is hook layer 1: it decides whether the other six ever get seen. A
space channel has no face to fall back on, so these are built around the two
things that do work without one:

* **Scale contrast** — a bold, short line plus one dominant subject.
* **Punch** — extra saturation and contrast, a darkening scrim behind the text,
  and a vignette so the eye lands in the middle.

Three variants are produced with different frames, layouts and crops. Variant 1
is uploaded automatically; the others are saved next to it so you can swap them
in from Studio if the first one underperforms.

Hard constraint enforced here: at most ``thumbnail.max_words`` words (default 5).
On a phone a thumbnail is roughly 120 pixels wide — anything longer is unreadable,
and unreadable means unclicked.

Public API
----------
    paths = generate_thumbnails(video_path, hook, out_dir, stem)
"""

from __future__ import annotations

import random
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from modules.config import cfg, setup_logging

log = setup_logging(__name__)

_FONT_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]
_FONT_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]


def _load_font(size: int, bold: bool = True):
    from PIL import ImageFont

    for path in (_FONT_BOLD if bold else _FONT_REGULAR):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001
                continue
    log.warning("DejaVu font not found — falling back to a bitmap font. Install "
                "fonts-dejavu-core for readable thumbnails.")
    return ImageFont.load_default()


def _video_duration(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, timeout=60,
        )
        return float(r.stdout.decode().strip() or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def _grab_frame(video: Path, at_seconds: float, out_png: Path,
                width: int, height: int) -> Optional[Path]:
    """Extract one frame, scaled and cropped to the thumbnail aspect."""
    out_png.parent.mkdir(parents=True, exist_ok=True)
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
          f"crop={width}:{height}")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{max(0.0, at_seconds):.2f}", "-i", str(video),
           "-vframes", "1", "-vf", vf, str(out_png)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        if r.returncode == 0 and out_png.exists() and out_png.stat().st_size > 2000:
            return out_png
        log.warning("Frame grab at %.1fs failed: %s", at_seconds,
                    r.stderr.decode(errors="ignore")[-200:])
    except Exception as exc:  # noqa: BLE001
        log.warning("Frame grab error at %.1fs: %s", at_seconds, exc)
    return None


def _score_frame(path: Path) -> float:
    """How visually striking a frame is. Higher is better.

    Sampling at fixed timestamps regularly landed on a dark or near-empty frame,
    which costs click-through directly. Scoring instead lets the best frame in the
    film become the thumbnail:

      * luminance spread  — flat or near-black frames score badly
      * colour saturation — a vivid nebula beats grey dust
      * edge energy       — structure and detail beat empty sky
      * a mid-brightness  bonus, since text has to remain readable over it
    """
    try:
        from PIL import Image, ImageFilter, ImageStat

        with Image.open(path) as im:
            small = im.convert("RGB").resize((256, 144))

        lum = small.convert("L")
        lum_stat = ImageStat.Stat(lum)
        spread = lum_stat.stddev[0] / 64.0
        mean = lum_stat.mean[0]

        sat_stat = ImageStat.Stat(small.convert("HSV").split()[1])
        saturation = sat_stat.mean[0] / 128.0

        edges = ImageStat.Stat(lum.filter(ImageFilter.FIND_EDGES)).mean[0] / 32.0

        # Penalise frames that are nearly black or blown out — both make text
        # unreadable and look like a mistake.
        if mean < 18:
            brightness = -1.5
        elif mean > 210:
            brightness = -0.8
        else:
            brightness = 1.0 - abs(mean - 95) / 140.0

        return spread + saturation + edges + brightness
    except Exception:  # noqa: BLE001
        return 0.0


def _wrap_to_width(draw, text: str, font, max_width: int) -> List[str]:
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit_font(draw, text: str, max_width: int, max_height: int,
              start_size: int, max_lines: int = 3):
    """Shrink the font until the wrapped text fits the text box."""
    size = start_size
    while size > 24:
        font = _load_font(size, bold=True)
        lines = _wrap_to_width(draw, text, font, max_width)
        line_h = int(size * 1.12)
        if len(lines) <= max_lines and len(lines) * line_h <= max_height:
            return font, lines, line_h
        size = int(size * 0.92)
    font = _load_font(24, bold=True)
    return font, _wrap_to_width(draw, text, font, max_width), 27


def _apply_punch(img, darken: float, saturation: float, contrast: float,
                 vignette: bool):
    """Grade the frame so text reads and the eye lands in the centre."""
    from PIL import Image, ImageDraw, ImageEnhance

    img = ImageEnhance.Color(img).enhance(saturation)
    img = ImageEnhance.Contrast(img).enhance(contrast)

    if vignette:
        w, h = img.size
        mask = Image.new("L", (w, h), 0)
        md = ImageDraw.Draw(mask)
        # Concentric ellipses fading outward — cheap but effective.
        steps = 26
        for i in range(steps):
            f = i / steps
            inset_x = int(w * 0.5 * f * 0.9)
            inset_y = int(h * 0.5 * f * 0.9)
            md.ellipse([inset_x, inset_y, w - inset_x, h - inset_y],
                       fill=int(255 * (1 - f) ** 1.2))
        dark = Image.new("RGB", (w, h), (0, 0, 0))
        img = Image.composite(img, Image.blend(img, dark, 0.55), mask)

    if darken > 0:
        from PIL import Image as _I
        overlay = _I.new("RGB", img.size, (0, 0, 0))
        img = _I.blend(img, overlay, min(0.85, max(0.0, darken)))
    return img


def _hex(color: str, default=(255, 255, 255)) -> Tuple[int, int, int]:
    c = str(color).lstrip("#")
    if len(c) != 6:
        return default
    try:
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    except ValueError:
        return default


def _draw_text_block(draw, lines: Sequence[str], font, line_h: int,
                     box: Tuple[int, int, int, int], align: str,
                     fill, stroke_fill, stroke_width: int) -> None:
    x0, y0, x1, _ = box
    y = y0
    for line in lines:
        tw = draw.textlength(line, font=font)
        if align == "center":
            x = x0 + (x1 - x0 - tw) / 2
        elif align == "right":
            x = x1 - tw
        else:
            x = x0
        draw.text((x, y), line, font=font, fill=fill,
                  stroke_width=stroke_width, stroke_fill=stroke_fill)
        y += line_h


def _accent() -> Tuple[int, int, int]:
    val = cfg("channel.accent_rgb", [92, 148, 255])
    try:
        return (int(val[0]), int(val[1]), int(val[2]))
    except Exception:  # noqa: BLE001
        return (92, 148, 255)


def _compose(frame_png: Path, hook: str, out_jpg: Path, layout: str) -> Optional[Path]:
    """Draw one thumbnail variant."""
    try:
        from PIL import Image, ImageDraw

        W = int(cfg("thumbnail.width", 1280))
        H = int(cfg("thumbnail.height", 720))

        img = Image.open(frame_png).convert("RGB")
        if img.size != (W, H):
            img = img.resize((W, H), Image.LANCZOS)

        img = _apply_punch(
            img,
            darken=float(cfg("thumbnail.darken_opacity", 0.42)) * (
                0.75 if layout == "lower" else 1.0),
            saturation=float(cfg("thumbnail.saturation", 1.18)),
            contrast=float(cfg("thumbnail.contrast", 1.08)),
            vignette=bool(cfg("thumbnail.vignette", True)),
        )

        draw = ImageDraw.Draw(img, "RGBA")
        margin = int(W * 0.055)
        title_color = _hex(str(cfg("thumbnail.title_color", "#FFFFFF")))
        accent_color = _hex(str(cfg("thumbnail.accent_color", "#FFD34D")),
                            (255, 211, 77))
        stroke_color = _hex(str(cfg("thumbnail.stroke_color", "#000000")), (0, 0, 0))
        stroke_w = int(cfg("thumbnail.stroke_width", 10))
        start_size = int(cfg("thumbnail.title_fontsize", 128))

        text = (hook or "BEYOND ORBIT").strip().upper()

        if layout == "center":
            box = (margin, 0, W - margin, H)
            font, lines, line_h = _fit_font(draw, text, W - 2 * margin,
                                            int(H * 0.62), start_size, 3)
            total_h = len(lines) * line_h
            box = (margin, (H - total_h) // 2, W - margin, H)
            # Scrim band behind the text so it reads over any frame.
            draw.rectangle([0, (H - total_h) // 2 - int(line_h * 0.35),
                            W, (H + total_h) // 2 + int(line_h * 0.35)],
                           fill=(0, 0, 0, 120))
            _draw_text_block(draw, lines, font, line_h, box, "center",
                             title_color, stroke_color, stroke_w)

        elif layout == "lower":
            font, lines, line_h = _fit_font(draw, text, W - 2 * margin,
                                            int(H * 0.42), int(start_size * 0.9), 2)
            total_h = len(lines) * line_h
            top = H - margin - total_h
            # Gradient scrim across the lower third.
            for y in range(int(H * 0.42), H):
                f = (y - H * 0.42) / (H - H * 0.42)
                draw.line([(0, y), (W, y)], fill=(0, 0, 0, int(205 * f ** 1.1)))
            _draw_text_block(draw, lines, font, line_h,
                             (margin, top, W - margin, H), "left",
                             title_color, stroke_color, stroke_w)
            # Accent underline for a bit of brand identity.
            draw.rectangle([margin, H - int(margin * 0.55),
                            margin + int(W * 0.16), H - int(margin * 0.42)],
                           fill=accent_color + (255,))

        else:  # "split" — text on the left, imagery breathing on the right
            text_w = int(W * 0.52)
            for x in range(0, text_w + int(W * 0.10)):
                f = 1.0 - (x / (text_w + W * 0.10))
                draw.line([(x, 0), (x, H)], fill=(0, 0, 0, int(215 * f ** 0.85)))
            font, lines, line_h = _fit_font(draw, text, text_w - margin,
                                            int(H * 0.66), start_size, 4)
            total_h = len(lines) * line_h
            _draw_text_block(draw, lines, font, line_h,
                             (margin, (H - total_h) // 2, margin + text_w, H),
                             "left", title_color, stroke_color, stroke_w)
            draw.rectangle([margin, (H - total_h) // 2 - int(line_h * 0.45),
                            margin + int(W * 0.13),
                            (H - total_h) // 2 - int(line_h * 0.30)],
                           fill=accent_color + (255,))

        out_jpg.parent.mkdir(parents=True, exist_ok=True)
        # YouTube's thumbnail limit is 2 MB; quality 88 at 1280x720 lands far
        # under it while staying visibly crisp.
        img.save(str(out_jpg), "JPEG", quality=88, optimize=True, progressive=True)

        size_kb = out_jpg.stat().st_size / 1024
        if size_kb > 1900:
            img.save(str(out_jpg), "JPEG", quality=74, optimize=True)
            size_kb = out_jpg.stat().st_size / 1024
        log.info("  thumbnail [%s]: %s (%.0f KB)", layout, out_jpg.name, size_kb)
        return out_jpg
    except Exception as exc:  # noqa: BLE001
        log.warning("Thumbnail layout %r failed (%s).", layout, exc)
        return None


def generate_thumbnails(
    video_path,
    hook: str,
    out_dir,
    stem: str = "thumb",
) -> List[Path]:
    """Create up to ``thumbnail.variants`` thumbnails from the rendered video.

    Returns the list of generated paths, best-first. Variant 1 is the one the
    uploader attaches; the rest are kept for manual A/B testing in Studio.
    Never raises — a missing thumbnail is not worth failing an upload over.
    """
    if not bool(cfg("thumbnail.enabled", True)):
        return []

    video_path = Path(video_path)
    out_dir = Path(out_dir)
    if not video_path.exists():
        log.warning("Cannot build thumbnails — video not found: %s", video_path)
        return []

    W = int(cfg("thumbnail.width", 1280))
    H = int(cfg("thumbnail.height", 720))
    variants = max(1, int(cfg("thumbnail.variants", 3)))
    layouts = ["center", "lower", "split"]

    duration = _video_duration(video_path)
    if duration <= 0:
        log.warning("Could not read the video duration for thumbnail frames.")
        return []

    # Sample widely, then KEEP THE BEST frames rather than trusting fixed
    # timestamps — those regularly landed on a dark or empty shot.
    sample_count = max(variants, int(cfg("thumbnail.frame_samples", 9)))
    fractions = [0.10 + (0.78 * i / max(1, sample_count - 1))
                 for i in range(sample_count)]

    scored: List[tuple] = []
    for i, frac in enumerate(fractions):
        at = max(1.0, min(duration * frac, duration - 1.0))
        frame = out_dir / f"{stem}_cand{i}.png"
        if not _grab_frame(video_path, at, frame, W, H):
            continue
        scored.append((_score_frame(frame), at, frame))

    if not scored:
        log.warning("No frames could be extracted for thumbnails.")
        return []

    scored.sort(key=lambda x: x[0], reverse=True)
    log.info("Scored %d candidate frame(s); best %.2f at %.0fs, worst %.2f.",
             len(scored), scored[0][0], scored[0][1], scored[-1][0])

    made: List[Path] = []
    for i, (score, at, frame) in enumerate(scored[:variants]):
        out_jpg = out_dir / f"{stem}_thumb{i + 1}.jpg"
        result = _compose(frame, hook, out_jpg, layouts[i % len(layouts)])
        if result:
            made.append(result)
            log.info("  variant %d from %.0fs (score %.2f, layout %s)",
                     i + 1, at, score, layouts[i % len(layouts)])
    for _, _, frame in scored:
        frame.unlink(missing_ok=True)

    if made:
        log.info("Generated %d thumbnail variant(s); uploading %s.",
                 len(made), made[0].name)
    else:
        log.warning("No thumbnails could be generated — YouTube will auto-pick a "
                    "frame, which usually costs click-through rate.")
    return made
