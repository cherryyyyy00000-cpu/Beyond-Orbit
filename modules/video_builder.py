"""Renders the 16:9 long-form documentary with ffmpeg.

Why this is built in three passes instead of one big filter graph
----------------------------------------------------------------
A 12-minute documentary with a visual change every 4-8 seconds is roughly 120
shots. Feeding 120 inputs into a single ``-filter_complex`` would produce a
command line tens of kilobytes long and an ffmpeg process that needs far more
memory than a free runner has. So instead:

    Pass 1  render each shot on its own to a normalised segment  <- the encode
    Pass 2  concat the segments with ``-c copy``                 <- no re-encode
    Pass 3  mux narration + music with ``-c:v copy``             <- no re-encode

The video is therefore encoded exactly **once**, which matters a great deal when
you have no GPU and a 6-hour job limit.

Retention features
------------------
* **Pattern interrupts.** Shot length is randomised between
  ``video.shot_seconds_min`` and ``video.shot_seconds_max``, so the frame never
  sits still long enough for attention to drift.
* **Ken Burns.** Still images get a slow zoom/pan, so a documentary built largely
  from NASA photographs never looks like a slideshow.
* **A consistent grade.** NASA assets come from dozens of instruments and look
  wildly different. A light contrast/saturation pass makes them feel like one
  film.

Nothing here is allowed to fail the run: the shot builder falls back to generated
starfields, and the renderer degrades to a solid colour rather than returning
nothing.

Public API
----------
    path = build_documentary(audio, assets, title, out_path, ass_path=None)
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from modules.config import CACHE_DIR, cfg, resolution, setup_logging

log = setup_logging(__name__)

_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_MUSIC_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


# ---------------------------------------------------------------------------
# ffprobe helpers
# ---------------------------------------------------------------------------
def _duration(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, timeout=60,
        )
        return float(r.stdout.decode().strip() or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def _dimensions(path: Path) -> Tuple[int, int]:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=s=x:p=0", str(path)],
            capture_output=True, timeout=60,
        )
        w, h = r.stdout.decode().strip().split("x")[:2]
        return int(w), int(h)
    except Exception:  # noqa: BLE001
        return 0, 0


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# ---------------------------------------------------------------------------
# Shot planning
# ---------------------------------------------------------------------------
@dataclass
class Shot:
    """One visual beat of the finished film."""

    src: Path
    is_video: bool
    duration: float
    start: float = 0.0           # in-point for video sources
    motion: str = "zoom_in"      # Ken Burns mode for stills


_MOTIONS = ("zoom_in", "zoom_out", "pan_right", "pan_left", "pan_down")


def plan_shots(assets: Sequence, total_seconds: float,
               rng: Optional[random.Random] = None,
               best_first: bool = False,
               loop_seamless: bool = False) -> List[Shot]:
    """Lay out shots covering ``total_seconds``.

    Assets are cycled in a shuffled order so the same visual never appears twice
    in a row, and video sources get a different in-point each time they come
    round — so reuse is not visible as repetition.

    Args:
        best_first: open on the highest-resolution asset. The first frame is what
            decides whether anyone watches at all — on a Short it is the frame
            people judge while scrolling, and on long-form it sits behind the
            first three seconds of narration.
        loop_seamless: end on the SAME asset the video opened with, so a Short
            loops without a visible cut. Replays count as views, and a clean loop
            is one of the few reliable ways to earn them.
    """
    rng = rng or random.Random()
    lo = float(cfg("video.shot_seconds_min", 4.0))
    hi = float(cfg("video.shot_seconds_max", 8.0))
    if hi < lo:
        lo, hi = hi, lo

    usable = [a for a in assets if getattr(a, "path", None) and Path(a.path).exists()]
    if not usable:
        return []

    # Open on the sharpest asset available.
    opener = None
    if best_first and usable:
        opener = max(usable, key=lambda a: (getattr(a, "width", 0) or 0)
                     * (getattr(a, "height", 0) or 0))

    order: List = []
    shots: List[Shot] = []
    elapsed = 0.0
    last_src: Optional[Path] = None
    # Cache probes — ffprobe per shot on 120 shots is a needless minute of I/O.
    dur_cache: Dict[str, float] = {}

    while elapsed < total_seconds:
        if not order:
            order = list(usable)
            rng.shuffle(order)
            # Avoid a back-to-back repeat across the cycle boundary.
            if len(order) > 1 and last_src and Path(order[0].path) == last_src:
                order.append(order.pop(0))

        if opener is not None and not shots:
            asset = opener
            if asset in order:
                order.remove(asset)
        else:
            asset = order.pop(0)
        src = Path(asset.path)
        want = round(rng.uniform(lo, hi), 2)
        want = min(want, total_seconds - elapsed)
        if want < 1.0:
            break

        is_video = getattr(asset, "kind", "") == "video" or src.suffix.lower() in _VIDEO_EXTS
        start = 0.0
        if is_video:
            key = str(src)
            if key not in dur_cache:
                dur_cache[key] = _duration(src)
            src_dur = dur_cache[key]
            if src_dur <= 0.6:
                # Unreadable or near-empty file — treat it as a still.
                is_video = False
            elif src_dur > want + 0.5:
                start = round(rng.uniform(0, max(0.0, src_dur - want - 0.3)), 2)
            else:
                # Shorter than the shot: use what exists and let it loop.
                want = min(want, max(1.0, src_dur))

        shots.append(Shot(
            src=src, is_video=is_video, duration=want, start=start,
            motion=rng.choice(_MOTIONS),
        ))
        last_src = src
        elapsed += want

    # Close on the opening asset so a Short loops without a visible cut.
    if loop_seamless and len(shots) > 2:
        first = shots[0]
        shots[-1] = Shot(src=first.src, is_video=first.is_video,
                         duration=shots[-1].duration, start=first.start,
                         motion=first.motion)
        log.info("Seamless loop: closing on the opening shot (%s).", first.src.name)

    log.info("Shot plan: %d shots over %.1f min (avg %.1fs each).",
             len(shots), elapsed / 60.0, elapsed / max(1, len(shots)))
    return shots


# ---------------------------------------------------------------------------
# Per-shot filter construction
# ---------------------------------------------------------------------------
def _grade_filter() -> str:
    """A light, consistent grade so mixed NASA sources feel like one film."""
    grade = cfg("video.grade", {}) or {}
    contrast = float(grade.get("contrast", 1.06))
    saturation = float(grade.get("saturation", 1.10))
    brightness = float(grade.get("brightness", -0.01))
    gamma = float(grade.get("gamma", 1.0))
    return (f"eq=contrast={contrast}:saturation={saturation}"
            f":brightness={brightness}:gamma={gamma}")


def _kenburns_filter(motion: str, W: int, H: int, fps: int, duration: float) -> str:
    """Build a zoompan expression for a still image.

    ``zoompan`` works in INPUT coordinates, so the image is first scaled to cover
    a frame larger than the output — that headroom is what the pan moves through.
    The zoom ramps linearly on the output frame counter ``on`` rather than using
    the ``zoom+inc`` accumulator, which drifts at different frame rates.
    """
    z_max = max(1.01, float(cfg("video.ken_burns_zoom", 1.12)))
    frames = max(2, int(round(duration * fps)))

    # Oversample so panning never runs out of pixels or shows an edge.
    scale = f"scale={int(W * 1.25)}:{int(H * 1.25)}:force_original_aspect_ratio=increase," \
            f"crop={int(W * 1.25)}:{int(H * 1.25)}"

    ramp_in = f"1+({z_max}-1)*on/{frames - 1}"
    ramp_out = f"{z_max}-({z_max}-1)*on/{frames - 1}"

    if motion == "zoom_out":
        z, x, y = ramp_out, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif motion == "pan_right":
        z = f"{z_max}"
        x, y = f"(iw-iw/zoom)*on/{frames - 1}", "ih/2-(ih/zoom/2)"
    elif motion == "pan_left":
        z = f"{z_max}"
        x, y = f"(iw-iw/zoom)*(1-on/{frames - 1})", "ih/2-(ih/zoom/2)"
    elif motion == "pan_down":
        z = f"{z_max}"
        x, y = "iw/2-(iw/zoom/2)", f"(ih-ih/zoom)*on/{frames - 1}"
    else:  # zoom_in
        z, x, y = ramp_in, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"

    return (f"{scale},zoompan=z='{z}':x='{x}':y='{y}'"
            f":d={frames}:s={W}x{H}:fps={fps},{_grade_filter()},setsar=1")


def _video_filter(W: int, H: int, fps: int) -> str:
    return (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},fps={fps},{_grade_filter()},setsar=1")


def _encode_args(crf: int, preset: str, fps: int) -> List[str]:
    """Encoder settings shared by EVERY segment.

    They must be byte-for-byte identical across segments, otherwise the concat
    demuxer's stream copy in pass 2 produces a broken file.
    """
    return [
        "-an",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-r", str(fps), "-g", str(fps * 2), "-keyint_min", str(fps),
        "-sc_threshold", "0",
        "-video_track_timescale", "90000",
    ]


def _render_shot(shot: Shot, index: int, W: int, H: int, fps: int,
                 crf: int, preset: str, work: Path,
                 overlay_png: Optional[Path] = None) -> Optional[Path]:
    """Render one shot to a normalised, audio-free segment.

    ``overlay_png`` composites a transparent PNG over the shot — used for the end
    card, which keeps real footage moving underneath instead of cutting to a
    static frame (a static outro is where viewers leave).
    """
    out = work / f"seg_{index:05d}.mp4"
    cmd: List[str] = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]

    if shot.is_video:
        # -ss BEFORE -i seeks fast; -stream_loop covers sources shorter than the shot.
        cmd += ["-stream_loop", "-1", "-ss", f"{shot.start:.2f}", "-i", str(shot.src)]
        vf = _video_filter(W, H, fps)
    else:
        cmd += ["-loop", "1", "-i", str(shot.src)]
        vf = (_kenburns_filter(shot.motion, W, H, fps, shot.duration)
              if bool(cfg("video.ken_burns", True))
              else f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                   f"crop={W}:{H},fps={fps},{_grade_filter()},setsar=1")

    if overlay_png and Path(overlay_png).exists():
        cmd += ["-loop", "1", "-i", str(overlay_png)]
        cmd += ["-filter_complex",
                f"[0:v]{vf}[bg];[1:v]format=rgba,scale={W}:{H}[ov];"
                f"[bg][ov]overlay=0:0:format=auto[vout]"]
        cmd += ["-map", "[vout]"]
    else:
        cmd += ["-vf", vf]

    cmd += ["-t", f"{shot.duration:.2f}"]
    cmd += _encode_args(crf, preset, fps)
    cmd += [str(out)]

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=900)
    except Exception as exc:  # noqa: BLE001
        log.warning("Shot %d render error (%s).", index, exc)
        return None
    if r.returncode != 0 or not out.exists():
        log.warning("Shot %d failed (%s): %s", index, shot.src.name,
                    r.stderr.decode(errors="ignore")[-260:])
        out.unlink(missing_ok=True)
        return None
    return out


# ---------------------------------------------------------------------------
# Concat + mux
# ---------------------------------------------------------------------------
def _concat_file(paths: Sequence[Path], listing: Path) -> None:
    q, esc = chr(39), chr(39) + chr(92) + chr(39) + chr(39)
    listing.write_text(
        "".join(f"file '{str(p.resolve()).replace(q, esc)}'\n" for p in paths),
        encoding="utf-8",
    )


def _concat_segments(segments: Sequence[Path], out_path: Path) -> bool:
    listing = out_path.with_suffix(".segments.txt")
    try:
        _concat_file(segments, listing)
        r = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c", "copy", "-movflags", "+faststart", str(out_path)],
            capture_output=True, timeout=1800,
        )
        if r.returncode != 0:
            log.error("Segment concat failed: %s",
                      r.stderr.decode(errors="ignore")[-400:])
            return False
        return out_path.exists()
    except Exception as exc:  # noqa: BLE001
        log.error("Segment concat error: %s", exc)
        return False
    finally:
        listing.unlink(missing_ok=True)


def pick_music() -> Optional[Path]:
    d = Path(cfg("video.music_dir", "assets/music"))
    if not d.exists():
        return None
    tracks = [p for p in d.iterdir() if p.suffix.lower() in _MUSIC_EXTS]
    return random.choice(tracks) if tracks else None


def _escape_sub_path(path: Path) -> str:
    p = str(path)
    return p.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _mux_audio(video: Path, narration: Path, out_path: Path,
               music: Optional[Path], burn_ass: Optional[Path],
               crf: int, preset: str) -> bool:
    """Attach narration (+ optional music) and finish the file.

    With no burned-in subtitles this is a stream copy of the video, so it costs
    seconds. If subtitles ARE burned in, the video has to be re-encoded — which
    is exactly why ``captions.burn_in`` defaults to false for long-form.
    """
    cmd: List[str] = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                      "-i", str(video), "-i", str(narration)]
    filters: List[str] = []
    idx_music = None
    if music and Path(music).exists():
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        idx_music = 2

    if idx_music is not None:
        vol = float(cfg("video.music_volume", 0.07))
        filters.append(f"[{idx_music}:a]volume={vol}[mus]")
        filters.append("[1:a][mus]amix=inputs=2:duration=first:dropout_transition=0[amix]")
        # -14 LUFS is the YouTube playback target: match it and the platform
        # leaves the mix alone instead of turning it down.
        filters.append("[amix]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    else:
        filters.append("[1:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")

    if burn_ass and Path(burn_ass).exists():
        filters.append(f"[0:v]subtitles='{_escape_sub_path(Path(burn_ass))}'[vout]")
        cmd += ["-filter_complex", ";".join(filters)]
        cmd += ["-map", "[vout]", "-map", "[aout]"]
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-profile:v", "high"]
    else:
        cmd += ["-filter_complex", ";".join(filters)]
        cmd += ["-map", "0:v:0", "-map", "[aout]", "-c:v", "copy"]

    cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-shortest", "-movflags", "+faststart", str(out_path)]

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=3600)
    except Exception as exc:  # noqa: BLE001
        log.error("Audio mux error: %s", exc)
        return False
    if r.returncode != 0:
        log.error("Audio mux failed: %s", r.stderr.decode(errors="ignore")[-500:])
        return False
    return out_path.exists()


# ---------------------------------------------------------------------------
# Last-resort background
# ---------------------------------------------------------------------------
def make_end_card(out_png: Path, W: int, H: int,
                  next_teaser: str = "") -> Optional[Path]:
    """A transparent end-card overlay for the final shot.

    Two reasons this exists. First, YouTube end screens can only be placed in the
    last 5-20 seconds, so the video has to actually reserve that time. Second, the
    ask to subscribe is the single biggest lever on subscriber conversion, and
    Beyond Orbit had none — the video simply stopped.

    It is an OVERLAY, not a static card: real footage keeps moving underneath,
    because cutting to a still frame at the end is where viewers leave.
    """
    try:
        from PIL import Image, ImageDraw

        out_png.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Darken the lower half so text reads over any footage.
        for y in range(int(H * 0.45), H):
            f = (y - H * 0.45) / (H - H * 0.45)
            draw.line([(0, y), (W, y)], fill=(0, 0, 0, int(215 * f ** 1.1)))

        margin = int(W * 0.06)
        accent = _accent_rgb()
        channel = str(cfg("channel.name", "Beyond Orbit"))
        cta = str(cfg("channel.cta", "Subscribe for a new deep-space story."))

        big = _load_font(int(W * 0.045))
        mid = _load_font(int(W * 0.028))
        small = _load_font(int(W * 0.020))

        y = int(H * 0.60)
        _text_with_shadow(draw, (margin, y), channel, big, fill=(255, 255, 255, 255))
        y += int(big.size * 1.35)
        draw.rectangle([margin, y, margin + int(W * 0.10), y + max(3, int(H * 0.006))],
                       fill=accent + (255,))
        y += int(H * 0.035)
        _text_with_shadow(draw, (margin, y), cta[:70], mid, fill=(235, 240, 255, 255))

        if next_teaser:
            y += int(mid.size * 1.5)
            _text_with_shadow(draw, (margin, y), f"Next: {next_teaser[:56]}",
                              small, fill=(200, 210, 235, 255))

        # Leave the right-hand third clear — that is where YouTube renders the
        # end-screen subscribe badge and video thumbnails.
        img.save(str(out_png), "PNG")
        log.info("End card created: %s", out_png.name)
        return out_png
    except Exception as exc:  # noqa: BLE001
        log.warning("End card generation failed (%s); continuing without it.", exc)
        return None


def _emergency_assets(work: Path, W: int, H: int, count: int = 5) -> List:
    """Generate starfield stills so a render is possible with no footage at all."""
    from modules.nasa_fetch import generate_starfield

    out = []
    for i in range(count):
        a = generate_starfield(work / f"starfield_{i}.png", W, H, seed=1000 + i)
        if a:
            out.append(a)
    return out


def _solid_fallback(narration: Path, out_path: Path, W: int, H: int,
                    fps: int, duration: float) -> bool:
    """Absolute floor: a dark frame plus the narration. Never returns nothing."""
    log.warning("Falling back to a plain background — no usable visuals.")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=0x05060C:s={W}x{H}:d={duration:.2f}:r={fps}",
        "-i", str(narration),
        "-filter_complex", "[1:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]",
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart", str(out_path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=3600)
        return r.returncode == 0 and out_path.exists()
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def build_documentary(
    narration_path,
    assets: Sequence,
    title: str,
    out_path,
    ass_path: Optional[Path] = None,
    music_path: Optional[Path] = None,
    size: Optional[Tuple[int, int]] = None,
    burn_subtitles: Optional[bool] = None,
    max_seconds: Optional[float] = None,
    outro_seconds: Optional[float] = None,
    end_card_seconds: Optional[float] = None,
    next_teaser: str = "",
    loop_seamless: bool = False,
) -> Optional[Path]:
    """Render a finished video.

    Despite the name this renders BOTH formats. The three-pass pipeline, the
    Ken Burns motion, the pattern-interrupt shot planning and the fallback chain
    are identical for a 16:9 documentary and a 9:16 Short — only the frame size
    and whether captions are burned in differ. Sharing it means a fix to the
    renderer benefits both.

    Args:
        narration_path: the edge-tts MP3; its length defines the video length.
        assets: Assets from modules.nasa_fetch (mix of stills and clips).
        title: used only for logging — the on-screen identity lives in the
            thumbnail and title, not burned into the frame.
        out_path: destination .mp4.
        ass_path: subtitles to burn in.
        music_path: optional bed; picked from assets/music if omitted.
        size: (width, height) override. Defaults to the configured long-form
            resolution; pass (1080, 1920) for a Short.
        burn_subtitles: force burning on/off. Long-form defaults to
            ``captions.burn_in`` (false — a .srt sidecar is uploaded instead),
            but a Short must have them burned in because it is watched muted.
        max_seconds / outro_seconds: override the long-form defaults.

    Returns the output path, or None if even the solid-colour fallback failed.
    """
    narration_path = Path(narration_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not ffmpeg_available():
        log.error("ffmpeg/ffprobe not found on PATH — cannot render. "
                  "Install with: apt-get install -y ffmpeg")
        return None

    W, H = size if size else resolution()
    W, H = W - W % 2, H - H % 2          # libx264 + yuv420p need even dimensions
    fps = int(cfg("video.fps", 30))
    crf = int(cfg("video.crf", 20))
    preset = str(cfg("video.preset", "fast"))
    max_seconds = (float(max_seconds) if max_seconds is not None
                   else float(cfg("video.max_minutes", 20)) * 60.0)
    outro = (float(outro_seconds) if outro_seconds is not None
             else float(cfg("video.outro_seconds", 6.0)))
    end_card_seconds = (float(end_card_seconds) if end_card_seconds is not None
                        else float(cfg("video.end_card_seconds", 0)))

    narr_dur = _duration(narration_path)
    if narr_dur <= 0:
        log.error("Could not read the narration duration — aborting render.")
        return None
    total = min(narr_dur + outro, max_seconds)
    if narr_dur + outro > max_seconds:
        log.warning("Narration is %.1f min; clamping the video to the %.0f min "
                    "video.max_minutes limit.", narr_dur / 60.0, max_seconds / 60.0)

    log.info("Rendering %dx%d @%dfps, %.1f min, preset=%s crf=%d",
             W, H, fps, total / 60.0, preset, crf)
    if (W * H) >= 3840 * 2160 and preset in ("medium", "slow", "slower", "veryslow"):
        log.warning("4K with preset=%r on a GPU-less runner can exceed the "
                    "6-hour job limit. Consider preset='fast' or resolution "
                    "'1440p'.", preset)

    work = Path(CACHE_DIR) / f"render_{out_path.stem}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    try:
        rng = random.Random(f"beyond-orbit::{out_path.stem}")

        usable = [a for a in assets
                  if getattr(a, "path", None) and Path(a.path).exists()]
        if not usable:
            log.warning("No footage supplied — generating starfields instead.")
            usable = _emergency_assets(work, W, H)

        shots = plan_shots(
            usable, total, rng,
            best_first=bool(cfg("video.best_shot_first", True)),
            loop_seamless=bool(loop_seamless),
        ) if usable else []
        if not shots:
            if _solid_fallback(narration_path, out_path, W, H, fps, total):
                log.info("Rendered with the plain fallback: %s", out_path.name)
                return out_path
            return None

        # --- End card: overlay the last N seconds -------------------------
        # YouTube end screens can only live in the last 5-20 seconds, so the
        # video has to reserve that window, and the subscribe ask has to exist at
        # all. Applied as an overlay so footage keeps moving underneath.
        end_card: Optional[Path] = None
        end_from_index = len(shots)
        if end_card_seconds and end_card_seconds > 0 and len(shots) > 1:
            end_card = make_end_card(work / "endcard.png", W, H,
                                     next_teaser=next_teaser or "")
            if end_card:
                acc = 0.0
                for i in range(len(shots) - 1, -1, -1):
                    acc += shots[i].duration
                    end_from_index = i
                    if acc >= end_card_seconds:
                        break
                log.info("End card over the final %d shot(s) (~%.0fs).",
                         len(shots) - end_from_index, acc)

        # --- Pass 1: encode each shot -------------------------------------
        segments: List[Path] = []
        for i, shot in enumerate(shots):
            seg = _render_shot(shot, i, W, H, fps, crf, preset, work,
                               overlay_png=(end_card if i >= end_from_index else None))
            if seg:
                segments.append(seg)
            if (i + 1) % 20 == 0:
                log.info("  encoded %d/%d shots...", i + 1, len(shots))

        if not segments:
            log.error("Every shot failed to encode.")
            if _solid_fallback(narration_path, out_path, W, H, fps, total):
                return out_path
            return None
        if len(segments) < len(shots):
            log.warning("%d/%d shots failed; the video will be shorter than the "
                        "narration and the tail will be trimmed.",
                        len(shots) - len(segments), len(shots))

        # --- Pass 2: concat (no re-encode) --------------------------------
        silent = work / "silent.mp4"
        if not _concat_segments(segments, silent):
            if _solid_fallback(narration_path, out_path, W, H, fps, total):
                return out_path
            return None

        # --- Pass 3: mux audio (stream copy unless burning subtitles) -----
        want_burn = (bool(burn_subtitles) if burn_subtitles is not None
                     else bool(cfg("captions.burn_in", False)))
        burn = ass_path if (ass_path and want_burn) else None
        if burn:
            log.info("Burning subtitles in — this forces a video re-encode.")
        if music_path is None:
            music_path = pick_music()
        if music_path:
            log.info("Background music: %s (volume %.2f)",
                     Path(music_path).name, float(cfg("video.music_volume", 0.07)))

        if not _mux_audio(silent, narration_path, out_path, music_path,
                          burn, crf, preset):
            log.error("Could not attach the audio.")
            return None

        size = out_path.stat().st_size
        final = _duration(out_path)
        w, h = _dimensions(out_path)
        log.info("Documentary rendered: %s (%dx%d, %.1f min, %.0f MB)",
                 out_path.name, w, h, final / 60.0, size / 1_048_576)
        return out_path

    finally:
        shutil.rmtree(work, ignore_errors=True)



def build_vertical(
    narration_path,
    assets: Sequence,
    title: str,
    out_path,
    ass_path: Optional[Path] = None,
    max_seconds: float = 62.0,
) -> Optional[Path]:
    """Render a 9:16 Short using the same pipeline as the documentary.

    Differences from long-form, all of which matter:
      * frame size comes from ``shorts.width``/``shorts.height`` (1080x1920),
      * captions are ALWAYS burned in — a Short is watched muted while scrolling,
      * no outro tail and no music bed, because 40 seconds has no room for either.
    """
    W = int(cfg("shorts.width", 1080))
    H = int(cfg("shorts.height", 1920))
    return build_documentary(
        narration_path, assets, title, out_path,
        ass_path=ass_path,
        music_path=None,
        size=(W, H),
        burn_subtitles=True,
        max_seconds=max_seconds,
        outro_seconds=0.6,
        # No end card on a Short — 20 seconds has no room for one, and the CTA is
        # already burned into the captions.
        end_card_seconds=0,
        # Close on the opening shot so the Short loops with no visible cut.
        # Replays count as views, and a clean loop is one of the few dependable
        # ways to earn them.
        loop_seamless=bool(cfg("shorts.seamless_loop", True)),
    )
