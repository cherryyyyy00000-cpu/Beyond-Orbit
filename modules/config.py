"""Centralized configuration, environment and logging for Beyond Orbit.

Secrets are ALWAYS read from environment variables (never hardcoded), so the
same code runs locally from a .env file and on GitHub Actions from repo secrets.

Usage:
    from modules.config import cfg, get_env, setup_logging, resolution
    log = setup_logging(__name__)
    width, height = resolution()
    key = get_env("GEMINI_API_KEY")          # optional -> may be None
    tok = get_env("YT_REFRESH_TOKEN", required=True)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# --- Optional .env support -------------------------------------------------
# python-dotenv is convenient locally but must never be *required* on CI, where
# variables are injected straight into the environment.
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass


# --- Paths -----------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.json"

OUTPUT_DIR = PROJECT_ROOT / "output"
ASSETS_DIR = PROJECT_ROOT / "assets"
CACHE_DIR = ASSETS_DIR / "cache"
FOOTAGE_DIR = ASSETS_DIR / "footage"
MUSIC_DIR = ASSETS_DIR / "music"
TOPICS_DIR = PROJECT_ROOT / "topics"

STATE_FILE = PROJECT_ROOT / "rotation_state.json"
SCHEDULE_FILE = PROJECT_ROOT / "schedule_state.json"


def ensure_dirs() -> None:
    """Create the runtime directory layout if it does not already exist."""
    for d in (OUTPUT_DIR, ASSETS_DIR, CACHE_DIR, FOOTAGE_DIR, MUSIC_DIR):
        d.mkdir(parents=True, exist_ok=True)


# --- config.json -----------------------------------------------------------
def _load_config() -> Dict[str, Any]:
    """Load config.json, returning {} (built-in defaults) if unusable."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        logging.warning("config.json not found at %s; using built-in defaults", CONFIG_FILE)
        return {}
    except json.JSONDecodeError as exc:
        logging.error("config.json is invalid JSON: %s", exc)
        return {}


CONFIG: Dict[str, Any] = _load_config()


def cfg(path: str, default: Any = None) -> Any:
    """Read a nested config value with dotted notation, e.g. cfg('video.fps')."""
    node: Any = CONFIG
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


# --- Environment helpers ---------------------------------------------------
def get_env(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    """Read an environment variable.

    Placeholder values from .env.example (anything starting with "your_") are
    treated as MISSING, so a half-filled .env fails loudly instead of sending
    garbage to an API.
    """
    value = os.environ.get(name, default)
    if value is not None and str(value).strip().lower().startswith("your_"):
        value = None if default is None else default
        if value is not None and str(value).strip().lower().startswith("your_"):
            value = None
    if required and (value is None or str(value).strip() == ""):
        raise RuntimeError(
            f"Required environment variable '{name}' is not set. "
            f"Add it to your .env file or to the repository's Actions secrets."
        )
    return value


def has_env(name: str) -> bool:
    """True only if the variable is set to a real (non-placeholder) value."""
    v = get_env(name)
    return bool(v and str(v).strip())


# --- Resolution ------------------------------------------------------------
_FALLBACK_RESOLUTIONS = {
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "4k": (3840, 2160),
    "2160p": (3840, 2160),
}


def resolution() -> Tuple[int, int]:
    """Return the long-form render size as (width, height).

    Resolved from, in priority order:
      1. the BO_RESOLUTION environment variable (handy for a one-off test run),
      2. video.resolution in config.json,
      3. 1440p.

    1440p is the default on purpose: YouTube encodes 1440p and above with VP9,
    which looks better than a 1080p upload even for viewers watching at 1080p,
    while costing roughly a third of 4K's encode time on a GPU-less runner.
    """
    name = str(get_env("BO_RESOLUTION") or cfg("video.resolution", "1440p")).strip().lower()
    table = cfg("video.resolutions", {}) or {}

    entry = table.get(name) or _FALLBACK_RESOLUTIONS.get(name)
    if not entry:
        logging.warning("Unknown video.resolution %r; falling back to 1440p.", name)
        entry = _FALLBACK_RESOLUTIONS["1440p"]
    try:
        w, h = int(entry[0]), int(entry[1])
    except Exception:  # noqa: BLE001
        w, h = _FALLBACK_RESOLUTIONS["1440p"]

    # libx264 with yuv420p requires even dimensions.
    return (w - w % 2, h - h % 2)


def resolution_name() -> str:
    """The configured resolution label, e.g. '1440p' — used in logs/manifest."""
    return str(get_env("BO_RESOLUTION") or cfg("video.resolution", "1440p")).strip().lower()


# --- Logging ---------------------------------------------------------------
def setup_logging(name: str = "beyondorbit", level: Optional[str] = None) -> logging.Logger:
    """Return a logger with a consistent, readable format.

    Level can be overridden with the LOG_LEVEL env var (default INFO).
    """
    log_level = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        logger.addHandler(handler)
    logger.setLevel(getattr(logging, log_level, logging.INFO))
    logger.propagate = False
    return logger
