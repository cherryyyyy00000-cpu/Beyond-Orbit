#!/usr/bin/env python3
"""Beyond Orbit — pre-flight setup check.

Run this BEFORE trusting the pipeline. It answers the questions that actually
break automated uploads, and it answers them from the live API rather than from
what you think you configured:

    1. Are the required secrets present?
    2. Does the refresh token still work?
    3. **Which YouTube channel does the token actually control?**  <- the big one.
       If the wrong channel was picked in the account chooser, every video
       silently uploads to that other channel instead.
    4. Which scopes were really granted? (Uploading needs youtube.upload;
       caption tracks and thumbnails need youtube.force-ssl.)
    5. Is the system able to render at all — ffmpeg, fonts, edge-tts?
    6. How much upload quota is left today?

Usage
-----
    python verify_setup.py

Exit code is 0 if everything required passed, 1 otherwise — so CI can gate on it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from typing import List, Optional, Tuple

# Import config first so a .env file is loaded before we look at the environment.
try:
    from modules.config import cfg, get_env, resolution, resolution_name
except Exception as exc:  # noqa: BLE001
    print(f"FATAL: could not import modules.config ({exc}).")
    print("Run this from the repository root: python verify_setup.py")
    sys.exit(1)


GREEN, RED, YELLOW, BLUE, DIM, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[94m", "\033[2m", "\033[0m"
)

_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
_FORCE_SSL_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"

_problems: List[str] = []
_warnings: List[str] = []


def ok(msg: str) -> None:
    print(f"  {GREEN}[PASS]{RESET} {msg}")


def bad(msg: str, fix: str = "") -> None:
    print(f"  {RED}[FAIL]{RESET} {msg}")
    if fix:
        print(f"         {DIM}fix: {fix}{RESET}")
    _problems.append(msg)


def warn(msg: str, fix: str = "") -> None:
    print(f"  {YELLOW}[WARN]{RESET} {msg}")
    if fix:
        print(f"         {DIM}{fix}{RESET}")
    _warnings.append(msg)


def info(msg: str) -> None:
    print(f"  {DIM}       {msg}{RESET}")


def header(title: str) -> None:
    print(f"\n{BLUE}== {title} {'=' * max(0, 58 - len(title))}{RESET}")


# ---------------------------------------------------------------------------
# 1. Secrets
# ---------------------------------------------------------------------------
def check_secrets() -> bool:
    header("1. Secrets")
    have_all = True
    for name in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"):
        val = get_env(name)
        if val:
            ok(f"{name} is set ({len(val)} chars, ends ...{val[-6:]})")
        else:
            bad(f"{name} is missing",
                "add it to .env locally, or to Settings -> Secrets and variables "
                "-> Actions in GitHub")
            have_all = False

    if get_env("PEXELS_API_KEY"):
        ok("PEXELS_API_KEY is set — filler B-roll available")
    else:
        info("PEXELS_API_KEY not set (optional; NASA is the primary source)")
    return have_all


# ---------------------------------------------------------------------------
# 1b. Gemini — actually CALL it
# ---------------------------------------------------------------------------
def check_gemini() -> bool:
    """Make a real Gemini request.

    Checking only that the variable is set is worse than useless: it reports
    "scripts enabled" while every run silently falls back to 2-minute templates
    and skips the topic. The only meaningful test is a live call.
    """
    header("2. Gemini (script generation)")

    if not get_env("GEMINI_API_KEY"):
        warn("GEMINI_API_KEY is not set",
             "Without it the offline template mode only reaches ~2-3 minutes, "
             "which is under script.min_publishable_minutes (6), so generate.py "
             "SKIPS every topic and produces nothing. "
             "Free key: https://aistudio.google.com/apikey")
        return False

    try:
        import google.generativeai as genai
        genai.configure(api_key=get_env("GEMINI_API_KEY"))
    except ImportError:
        bad("google-generativeai is not installed", "pip install -r requirements.txt")
        return False
    except Exception as exc:  # noqa: BLE001
        bad(f"Could not configure Gemini: {str(exc)[:180]}")
        return False

    # Which models does this key actually have?
    models: List[str] = []
    try:
        for m in genai.list_models():
            name = str(getattr(m, "name", "")).replace("models/", "")
            if name and "generateContent" in (
                    getattr(m, "supported_generation_methods", []) or []):
                models.append(name)
        if models:
            flash = [n for n in models if "flash" in n]
            ok(f"{len(models)} model(s) available to this key")
            info("flash models: " + (", ".join(sorted(flash, reverse=True)[:4])
                                     or "none"))
        else:
            bad("The key lists NO models that support generateContent",
                "the Generative Language API may not be enabled for this key's "
                "project — check https://aistudio.google.com/apikey")
            return False
    except Exception as exc:  # noqa: BLE001
        warn(f"Could not list models ({str(exc)[:140]}) — trying a call anyway.")

    # A real generation call, small enough to be cheap.
    order = sorted([n for n in models if "flash" in n], reverse=True) or models
    forced = get_env("GEMINI_MODEL")
    if forced:
        order = [forced] + [m for m in order if m != forced]

    for name in (order or ["gemini-2.5-flash"])[:3]:
        try:
            model = genai.GenerativeModel(name)
            resp = model.generate_content(
                "Reply with exactly: OK",
                generation_config={"temperature": 0.0, "max_output_tokens": 16},
            )
            text = (getattr(resp, "text", "") or "").strip()
            if text:
                ok(f"live generation works with '{name}' (replied {text[:20]!r})")
                info("Full ~12 minute scripts will be generated.")
                return True
            warn(f"'{name}' returned an empty response.")
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if any(k in msg.lower() for k in
                   ("429", "quota", "exceeded", "rate limit", "resource_exhausted")):
                bad(f"'{name}' is RATE-LIMITED / out of quota")
                info(f"exact error: {msg[:260]}")
                info("The free tier limits requests per minute AND per day.")
                info("If this is a brand-new key, the usual causes are:")
                info("  * the Generative Language API is not enabled for its project")
                info("  * the key was made in a project with billing restrictions")
                info("  * the free tier is unavailable in your region")
                info("Try creating the key fresh at https://aistudio.google.com/apikey")
            else:
                bad(f"'{name}' failed: {msg[:220]}")
    return False


# ---------------------------------------------------------------------------
# 2 + 3 + 4. Token, scopes, and WHICH channel
# ---------------------------------------------------------------------------
def check_youtube() -> Tuple[bool, Optional[str]]:
    header("3. YouTube credentials")

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        bad("Google API libraries are not installed",
            "pip install -r requirements.txt")
        return False, None

    cid, csecret, refresh = (get_env("YT_CLIENT_ID"), get_env("YT_CLIENT_SECRET"),
                             get_env("YT_REFRESH_TOKEN"))
    if not (cid and csecret and refresh):
        bad("Cannot test the token — secrets are incomplete")
        return False, None

    creds = Credentials(
        None, refresh_token=refresh,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=cid, client_secret=csecret,
    )
    try:
        creds.refresh(Request())
        ok("Refresh token works — a fresh access token was issued")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        bad(f"Token refresh FAILED: {msg[:180]}")
        if "invalid_grant" in msg.lower():
            info("'invalid_grant' almost always means one of these:")
            info("  * the OAuth app is still in Testing mode, so Google expired")
            info("    the token after 7 days -> Google Auth Platform -> Audience")
            info("    -> PUBLISH APP, then generate a new refresh token")
            info("  * the token was revoked, or the client secret was rotated")
        return False, None

    # --- Which scopes were ACTUALLY granted? -------------------------------
    header("4. Granted scopes")
    granted: List[str] = []
    try:
        url = ("https://oauth2.googleapis.com/tokeninfo?"
               + urllib.parse.urlencode({"access_token": creds.token}))
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read().decode())
        granted = str(data.get("scope", "")).split()
        for s in granted:
            print(f"  {DIM}       {s}{RESET}")
    except Exception as exc:  # noqa: BLE001
        warn(f"Could not query tokeninfo ({exc}) — checking capability directly instead.")

    if granted:
        if _UPLOAD_SCOPE in granted:
            ok("youtube.upload granted — video upload will work")
        else:
            bad("youtube.upload is NOT granted — uploads will be rejected",
                "re-run the OAuth Playground with this scope included")
        if _FORCE_SSL_SCOPE in granted or "https://www.googleapis.com/auth/youtube" in granted:
            ok("caption/thumbnail scope granted")
        else:
            warn("youtube.force-ssl is NOT granted",
                 "Video upload still works, but the .srt caption track and the "
                 "custom thumbnail will be skipped. To fix, redo the Playground "
                 "step with BOTH scopes:\n"
                 "           https://www.googleapis.com/auth/youtube.upload\n"
                 "           https://www.googleapis.com/auth/youtube.force-ssl")

    # --- WHICH CHANNEL? ----------------------------------------------------
    header("5. Which channel does this token control?")
    channel_title = None
    if granted and _FORCE_SSL_SCOPE not in granted and \
            "https://www.googleapis.com/auth/youtube" not in granted and \
            "https://www.googleapis.com/auth/youtube.readonly" not in granted:
        warn("Cannot check the channel — the token only has 'youtube.upload'",
             "youtube.upload is write-only: it can post a video but cannot READ "
             "channel info, so channels.list returns 403. Uploads will still\n"
             "         work, but you have NO confirmation of which channel they "
             "land on — and picking the wrong channel in the account chooser is\n"
             "         the most common setup mistake. Add youtube.force-ssl to fix "
             "this (and to enable caption tracks + custom thumbnails).")
        return True, None

    try:
        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        resp = yt.channels().list(part="snippet,statistics,status", mine=True).execute()
        items = resp.get("items") or []
        if not items:
            bad("The token authenticated but controls NO channel",
                "in the Google account chooser you probably picked a bare Google "
                "account rather than a channel. Redo the Playground step and "
                "select the Beyond Orbit channel.")
            return False, None

        ch = items[0]
        snip, stats = ch.get("snippet", {}), ch.get("statistics", {})
        channel_title = snip.get("title", "?")
        print(f"\n  {BLUE}>>> {channel_title} <<<{RESET}")
        info(f"channel id .... {ch.get('id')}")
        info(f"handle ........ {snip.get('customUrl', '(none set)')}")
        info(f"subscribers ... {stats.get('subscriberCount', '?')}")
        info(f"videos ........ {stats.get('videoCount', '?')}")
        info(f"url ........... https://www.youtube.com/channel/{ch.get('id')}")

        expected = str(cfg("channel.name", "Beyond Orbit")).strip().lower()
        actual = channel_title.strip().lower()
        print()
        if expected.replace(" ", "") == actual.replace(" ", ""):
            ok(f"This matches channel.name in config.json ({cfg('channel.name')})")
        else:
            bad(f"MISMATCH: the token controls '{channel_title}' but config.json "
                f"says '{cfg('channel.name')}'",
                "Every upload would go to the WRONG channel. Either redo the "
                "OAuth Playground step and pick the right channel in the account "
                "chooser, or update channel.name in config.json if you renamed "
                "the channel.")

        # Long uploads and custom thumbnails both need a verified channel.
        if not ch.get("status", {}).get("longUploadsStatus") in (None, "allowed", "eligible"):
            warn("This channel is not cleared for long uploads",
                 "verify the channel (Studio -> Settings -> Channel -> Feature "
                 "eligibility) or videos over 15 minutes will be rejected")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "insufficient authentication scopes" in msg.lower() or "403" in msg:
            warn("Cannot read the channel — insufficient scopes",
                 "the refresh token lacks youtube.force-ssl, so channel info "
                 "cannot be read. Uploads still work, but the channel is\n"
                 "         unverified. Redo the OAuth Playground step with BOTH "
                 "youtube.upload and youtube.force-ssl.")
            return True, None
        bad(f"channels().list failed: {msg[:200]}",
            "if this mentions 'accessNotConfigured', enable the YouTube Data API "
            "v3 for the project in the Cloud Console")
        return False, channel_title

    # --- Quota -------------------------------------------------------------
    header("6. Quota")
    info("A video upload costs 1,600 units; the default daily quota is 10,000")
    info("-> about 6 uploads per day for this Cloud project")
    info("Beyond Orbit posts 3 long-form + up to 2 Shorts per day at most,")
    info("so it fits — but give FactVault its OWN Cloud project, otherwise the")
    info("two channels share the same 10,000 units.")
    return True, channel_title


# ---------------------------------------------------------------------------
# 6. Render toolchain
# ---------------------------------------------------------------------------
def check_render() -> bool:
    header("7. Render toolchain")
    fine = True

    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        if path:
            try:
                r = subprocess.run([tool, "-version"], capture_output=True, timeout=20)
                ver = r.stdout.decode().splitlines()[0][:60]
                ok(f"{tool}: {ver}")
            except Exception:  # noqa: BLE001
                ok(f"{tool} found at {path}")
        else:
            bad(f"{tool} is not installed",
                "apt-get install -y ffmpeg   (the workflows do this automatically)")
            fine = False

    # zoompan drives the Ken Burns effect on still images.
    if shutil.which("ffmpeg"):
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                               capture_output=True, timeout=30)
            filters = r.stdout.decode()
            for filt in ("zoompan", "subtitles", "loudnorm"):
                if filt in filters:
                    ok(f"ffmpeg filter '{filt}' available")
                else:
                    warn(f"ffmpeg filter '{filt}' is missing",
                         "this build of ffmpeg is missing a feature the renderer "
                         "uses; install the full distro package")
        except Exception:  # noqa: BLE001
            pass

    fonts = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ]
    import os
    if any(os.path.exists(f) for f in fonts):
        ok("DejaVu font found (thumbnails + Short captions)")
    else:
        warn("DejaVu font not found",
             "apt-get install -y fonts-dejavu-core — otherwise text falls back "
             "to a tiny bitmap font and thumbnails look broken")

    try:
        import edge_tts  # noqa: F401
        ok("edge-tts importable")
    except ImportError:
        bad("edge-tts is not installed", "pip install -r requirements.txt")
        fine = False

    try:
        from PIL import Image  # noqa: F401
        ok("Pillow importable")
    except ImportError:
        bad("Pillow is not installed", "pip install -r requirements.txt")
        fine = False
    return fine


# ---------------------------------------------------------------------------
# 7. Content + config
# ---------------------------------------------------------------------------
def check_content() -> bool:
    header("8. Content and config")
    fine = True
    try:
        from modules.topic_source import load_topics
        topics = load_topics()
        if topics:
            ok(f"topic bank loads: {len(topics)} topics available")
        else:
            bad("the topic bank is empty or unreadable",
                "check topics/space_bank.json")
            fine = False
    except Exception as exc:  # noqa: BLE001
        bad(f"topic bank failed to load: {exc}")
        fine = False

    try:
        import json as _json
        from pathlib import Path

        from modules.config import STATE_FILE
        used = 0
        if Path(STATE_FILE).exists():
            used = len(_json.loads(Path(STATE_FILE).read_text())
                       .get("used_topic_ids", []))
        info(f"topics already used: {used}")
    except Exception:  # noqa: BLE001
        pass

    w, h = resolution()
    info(f"render resolution: {resolution_name()} = {w}x{h} @ "
         f"{cfg('video.fps', 30)}fps, preset={cfg('video.preset')} "
         f"crf={cfg('video.crf')}")
    if (w * h) >= 3840 * 2160 and str(cfg("video.preset")) in (
            "medium", "slow", "slower", "veryslow"):
        warn("4K with a slow preset on a GPU-less runner may exceed the 6-hour "
             "job limit", "use preset 'fast', or resolution '1440p'")
    return fine


# ---------------------------------------------------------------------------
def main() -> int:
    print(f"{BLUE}Beyond Orbit — setup verification{RESET}")
    print(f"{DIM}Checks the things that actually break automated uploads.{RESET}")

    check_secrets()
    check_gemini()
    check_youtube()
    check_render()
    check_content()

    header("Summary")
    if not _problems and not _warnings:
        print(f"  {GREEN}Everything passed. You are ready to run the pipeline.{RESET}")
        return 0
    if _problems:
        print(f"  {RED}{len(_problems)} blocking problem(s):{RESET}")
        for p in _problems:
            print(f"    - {p}")
    if _warnings:
        print(f"  {YELLOW}{len(_warnings)} warning(s) (not blocking):{RESET}")
        for w in _warnings:
            print(f"    - {w}")
    print()
    return 1 if _problems else 0


if __name__ == "__main__":
    sys.exit(main())
