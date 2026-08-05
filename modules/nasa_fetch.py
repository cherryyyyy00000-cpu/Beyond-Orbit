"""Public-domain space footage and stills for Beyond Orbit.

This is the module that makes the whole channel viable. NASA content generally is
NOT subject to copyright in the United States, and the NASA Scientific
Visualization Studio explicitly places its material in the public domain. That
gives us broadcast-quality 4K space imagery with effectively zero Content ID
risk — something no other faceless niche gets for free.

Sources, in priority order
--------------------------
1. ``images-api.nasa.gov`` — the NASA Image and Video Library search API. Free,
   needs NO key, and already mirrors a great deal of SVS and JPL material. This
   is the primary and fully-supported path.
2. NASA SVS — optional secondary source for native 4K animations. Its JSON
   endpoint is not formally documented as a stable public API, so the parsing
   here is deliberately defensive: any unexpected response is skipped silently
   rather than failing the run.
3. Pexels — only used as filler when the NASA sources return too few usable
   assets, and only if ``PEXELS_API_KEY`` is set.
4. A procedurally generated starfield — the last-resort fallback so a render can
   never fail for lack of visuals.

Two hard licensing rules are enforced here
------------------------------------------
* **The NASA logo, insignia, "meatball" and "worm" are blocked.** NASA material
  is otherwise free to reuse, but its identifiers may not be used in a way that
  implies NASA endorses this channel. See ``nasa.block_keywords`` in config.json.
* **Assets whose metadata names a third-party rights holder are skipped.** Some
  NASA pages embed licensed music or contractor-shot footage that NASA does not
  own. When in doubt, this module drops the asset.

Public API
----------
    assets = fetch_assets(queries, want=14, dest_dir=...)
    lines  = attribution_lines(assets)
"""

from __future__ import annotations

import json
import random
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from modules.config import cfg, get_env, setup_logging

log = setup_logging(__name__)

_IMAGES_SEARCH = "https://images-api.nasa.gov/search"
_IMAGES_ASSET = "https://images-api.nasa.gov/asset/"
_SVS_SEARCH = "https://svs.gsfc.nasa.gov/api/search/"
_PEXELS_VIDEO = "https://api.pexels.com/videos/search"

_UA = "BeyondOrbit/1.0 (+educational space documentary channel)"

# Per-file download ceilings. A 4K NASA "~orig.mp4" can be several hundred MB;
# free runners have ~14 GB of disk and we pull a dozen or more assets per video,
# so we cap each file and prefer a large-but-sane variant over the true original.
_MAX_VIDEO_BYTES = 220 * 1024 * 1024
_MAX_IMAGE_BYTES = 40 * 1024 * 1024
_MIN_USABLE_BYTES = 30 * 1024

# Phrases that indicate someone other than NASA owns the material. Conservative
# on purpose: a false skip costs us one clip, a false accept costs monetisation.
_THIRD_PARTY_MARKERS = (
    "copyright",
    "\u00a9",
    "(c)",
    "all rights reserved",
    "used with permission",
    "rights managed",
    "getty",
    "reuters",
    "associated press",
    "shutterstock",
    "adobe stock",
)

# Credit strings. NASA does not require attribution, but crediting is good
# practice and ESA/Hubble/ESO material is CC BY, where it IS required.
_CREDIT_BY_CENTER = {
    "GSFC": "NASA/Goddard Space Flight Center",
    "JPL": "NASA/JPL-Caltech",
    "JPL-Caltech": "NASA/JPL-Caltech",
    "HQ": "NASA",
    "MSFC": "NASA/Marshall Space Flight Center",
    "JSC": "NASA/Johnson Space Center",
    "ARC": "NASA/Ames Research Center",
    "KSC": "NASA/Kennedy Space Center",
    "LaRC": "NASA/Langley Research Center",
    "STScI": "NASA/ESA/STScI",
}


@dataclass
class Asset:
    """One downloaded visual, plus everything needed to credit it."""

    path: Path
    kind: str = "image"          # "image" | "video"
    title: str = ""
    asset_id: str = ""
    source: str = "images_api"   # images_api | svs | pexels | generated
    credit: str = "NASA"
    width: int = 0
    height: int = 0
    query: str = ""

    @property
    def is_video(self) -> bool:
        return self.kind == "video"


# ---------------------------------------------------------------------------
# HTTP helpers (requests is in requirements, but we degrade to urllib)
# ---------------------------------------------------------------------------
def _get_json(url: str, params: Optional[Dict] = None,
              headers: Optional[Dict] = None, timeout: int = 30) -> Optional[dict]:
    """GET and parse JSON. Returns None on ANY failure — never raises."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    hdrs = {"User-Agent": _UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    try:
        import requests

        resp = requests.get(url, headers=hdrs, timeout=timeout)
        if resp.status_code != 200:
            log.warning("GET %s -> HTTP %s", url.split("?")[0], resp.status_code)
            return None
        return resp.json()
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("GET %s failed: %s", url.split("?")[0], exc)
        return None

    try:  # urllib fallback
        import urllib.request

        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        log.warning("GET %s failed: %s", url.split("?")[0], exc)
        return None


def _download(url: str, dest: Path, max_bytes: int, timeout: int = 240) -> bool:
    """Stream a file to disk, aborting if it exceeds max_bytes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        import requests

        with requests.get(url, headers={"User-Agent": _UA},
                          stream=True, timeout=timeout) as r:
            if r.status_code != 200:
                log.warning("Download %s -> HTTP %s", Path(url).name, r.status_code)
                return False
            declared = int(r.headers.get("Content-Length") or 0)
            if declared and declared > max_bytes:
                log.info("Skipping %s (%.0f MB > cap)", Path(url).name,
                         declared / 1_048_576)
                return False
            written = 0
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(1024 * 256):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        log.info("Aborting %s — exceeded size cap.", Path(url).name)
                        fh.close()
                        dest.unlink(missing_ok=True)
                        return False
                    fh.write(chunk)
        return dest.exists() and dest.stat().st_size > _MIN_USABLE_BYTES
    except Exception as exc:  # noqa: BLE001
        log.warning("Download failed for %s: %s", Path(url).name, exc)
        dest.unlink(missing_ok=True)
        return False


# ---------------------------------------------------------------------------
# Licensing / safety filters
# ---------------------------------------------------------------------------
def _blocked_keywords() -> List[str]:
    kws = cfg("nasa.block_keywords",
              ["logo", "insignia", "meatball", "seal", "emblem", "worm"]) or []
    return [str(k).lower() for k in kws]


def _is_blocked(*texts: object) -> Optional[str]:
    """Return the offending keyword if any text mentions a NASA identifier.

    Using the NASA logo/insignia would imply NASA endorses this channel, which is
    not permitted. We would rather lose a clip than imply a false endorsement.
    """
    blob = " ".join(str(t).lower() for t in texts if t)
    for kw in _blocked_keywords():
        # Word-boundary match so "worm" does not trip on "wormhole" and
        # "seal" does not trip on "sealed".
        if re.search(rf"\b{re.escape(kw)}\b", blob):
            return kw
    return None


def _third_party_rights(*texts: object) -> Optional[str]:
    """Return the offending marker if the metadata names a non-NASA rights holder."""
    blob = " ".join(str(t).lower() for t in texts if t)
    for marker in _THIRD_PARTY_MARKERS:
        if marker in blob:
            # "copyright" appearing alongside an explicit public-domain or
            # no-copyright statement is fine — that is NASA's own boilerplate.
            if marker in ("copyright", "\u00a9", "(c)") and any(
                ok in blob for ok in ("public domain", "not copyrighted",
                                      "no copyright", "not subject to copyright")
            ):
                continue
            return marker
    return None


def _credit_for(center: object, secondary: object, title: object) -> str:
    """Build a human attribution line for an asset."""
    blob = f"{center} {secondary} {title}".lower()
    if "hubble" in blob or "esa" in blob:
        return "NASA/ESA Hubble Space Telescope"
    if "webb" in blob or "jwst" in blob:
        return "NASA/ESA/CSA James Webb Space Telescope"
    if "eso" in blob:
        return "ESO (CC BY 4.0)"
    key = str(center or "").strip()
    if key in _CREDIT_BY_CENTER:
        return _CREDIT_BY_CENTER[key]
    sec = str(secondary or "").strip()
    if sec:
        return sec[:120]
    return "NASA"


# ---------------------------------------------------------------------------
# Source 1 — images-api.nasa.gov (primary, no key required)
# ---------------------------------------------------------------------------
def _pick_asset_file(hrefs: Sequence[str], want_video: bool) -> Optional[str]:
    """Choose the best file variant from an /asset/{id} listing.

    NASA asset listings expose the same item at several sizes, suffixed
    ``~orig``, ``~large``, ``~medium``, ``~small``, ``~thumb``. For stills we
    want the biggest. For video the true original can be several hundred
    megabytes, so we prefer ``~large`` and only fall back to ``~orig``.
    """
    exts = (".mp4", ".mov", ".m4v") if want_video else (".jpg", ".jpeg", ".png", ".tif", ".tiff")
    cands = [h for h in hrefs if h.lower().split("?")[0].endswith(exts)]
    if not cands:
        return None

    order = ["~large", "~orig", "~medium"] if want_video else ["~orig", "~large", "~medium"]
    for token in order:
        for h in cands:
            if token in h.lower():
                return h
    return cands[0]


def _search_images_api(query: str, want_video: bool, limit: int) -> List[dict]:
    """Search the NASA library. Returns raw item dicts (already rights-filtered)."""
    data = _get_json(
        _IMAGES_SEARCH,
        {
            "q": query,
            "media_type": "video" if want_video else "image",
            "page_size": max(10, min(60, limit * 6)),
        },
    )
    if not isinstance(data, dict):
        return []
    items = (data.get("collection") or {}).get("items") or []
    if not isinstance(items, list):
        return []

    keep: List[dict] = []
    for item in items:
        meta_list = item.get("data") or []
        if not meta_list or not isinstance(meta_list, list):
            continue
        meta = meta_list[0] or {}
        title = meta.get("title") or ""
        nasa_id = meta.get("nasa_id") or ""
        if not nasa_id:
            continue

        desc = meta.get("description") or meta.get("description_508") or ""
        keywords = meta.get("keywords") or []
        secondary = meta.get("secondary_creator") or meta.get("photographer") or ""
        center = meta.get("center") or ""

        hit = _is_blocked(title, keywords, nasa_id)
        if hit:
            log.debug("Skip %s — blocked identifier %r", nasa_id, hit)
            continue
        marker = _third_party_rights(desc, secondary)
        if cfg("nasa.require_public_domain", True) and marker:
            log.debug("Skip %s — third-party rights marker %r", nasa_id, marker)
            continue

        keep.append(
            {
                "nasa_id": nasa_id,
                "title": str(title)[:180],
                "center": center,
                "secondary": secondary,
                "want_video": want_video,
            }
        )
    return keep


def _download_images_api_item(item: dict, dest_dir: Path, query: str) -> Optional[Asset]:
    """Resolve an item's real file URL and download it."""
    nasa_id = item["nasa_id"]
    listing = _get_json(_IMAGES_ASSET + urllib.parse.quote(nasa_id))
    if not isinstance(listing, dict):
        return None
    entries = (listing.get("collection") or {}).get("items") or []
    hrefs = [e.get("href") for e in entries if isinstance(e, dict) and e.get("href")]
    if not hrefs:
        return None

    want_video = bool(item.get("want_video"))
    url = _pick_asset_file(hrefs, want_video)
    if not url:
        return None
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]

    suffix = Path(urllib.parse.urlparse(url).path).suffix or (".mp4" if want_video else ".jpg")
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", nasa_id)[:60]
    dest = dest_dir / f"nasa_{safe_id}{suffix}"
    cap = _MAX_VIDEO_BYTES if want_video else _MAX_IMAGE_BYTES
    if not _download(url, dest, cap):
        return None

    width, height = _probe_dimensions(dest, want_video)
    if not want_video:
        min_w = int(cfg("nasa.min_image_width", 2560))
        if width and width < min_w:
            log.debug("Skip %s — %dpx wide, below %dpx minimum.", nasa_id, width, min_w)
            dest.unlink(missing_ok=True)
            return None

    return Asset(
        path=dest,
        kind="video" if want_video else "image",
        title=item.get("title", ""),
        asset_id=nasa_id,
        source="images_api",
        credit=_credit_for(item.get("center"), item.get("secondary"), item.get("title")),
        width=width,
        height=height,
        query=query,
    )


# ---------------------------------------------------------------------------
# Source 2 — NASA SVS (optional; response shape is NOT a documented stable API)
# ---------------------------------------------------------------------------
def _search_svs(query: str, limit: int) -> List[Asset]:
    """Best-effort SVS lookup.

    The SVS website exposes a JSON endpoint, but it is not published as a
    versioned public API, so its response shape may change without notice.
    Everything here is therefore wrapped in wide try/except and any surprise
    simply yields an empty list — images-api.nasa.gov remains the reliable path.
    """
    data = _get_json(_SVS_SEARCH, {"q": query, "limit": limit})
    if not isinstance(data, (dict, list)):
        return []
    try:
        rows = data.get("results", data.get("items", [])) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return []
        out: List[Asset] = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "")
            if _is_blocked(title):
                continue
            out.append(
                Asset(
                    path=Path(""),  # resolved by the caller if a media URL exists
                    kind="video",
                    title=title[:180],
                    asset_id=str(row.get("id") or ""),
                    source="svs",
                    credit="NASA/Goddard Space Flight Center Scientific Visualization Studio",
                    query=query,
                )
            )
        return out
    except Exception as exc:  # noqa: BLE001
        log.debug("SVS response not in an expected shape (%s) — skipping source.", exc)
        return []


# ---------------------------------------------------------------------------
# Source 3 — Pexels filler (optional, needs a free key)
# ---------------------------------------------------------------------------
def _fetch_pexels(query: str, dest_dir: Path, want: int) -> List[Asset]:
    key = get_env("PEXELS_API_KEY")
    if not key:
        return []
    data = _get_json(
        _PEXELS_VIDEO,
        {"query": query, "orientation": "landscape", "per_page": 20, "size": "large"},
        headers={"Authorization": key},
    )
    if not isinstance(data, dict):
        return []
    vids = data.get("videos") or []
    random.shuffle(vids)
    out: List[Asset] = []
    for v in vids:
        if len(out) >= want:
            break
        files = [
            f for f in (v.get("video_files") or [])
            if f.get("file_type") == "video/mp4" and f.get("link")
        ]
        if not files:
            continue
        # Prefer something close to 1440p tall-side without going enormous.
        files.sort(key=lambda f: abs((f.get("height") or 0) - 1440))
        dest = dest_dir / f"pexels_{v.get('id', 'clip')}.mp4"
        if not _download(files[0]["link"], dest, _MAX_VIDEO_BYTES):
            continue
        out.append(
            Asset(
                path=dest,
                kind="video",
                title=str(v.get("url") or "Pexels clip")[:180],
                asset_id=str(v.get("id") or ""),
                source="pexels",
                credit=f"Pexels — {v.get('user', {}).get('name', 'unknown')}",
                width=int(files[0].get("width") or 0),
                height=int(files[0].get("height") or 0),
                query=query,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Source 4 — generated starfield (last resort, always succeeds)
# ---------------------------------------------------------------------------
def generate_starfield(dest: Path, width: int, height: int,
                       seed: Optional[int] = None) -> Optional[Asset]:
    """Draw a plausible starfield with PIL so a render is never blocked."""
    try:
        from PIL import Image, ImageDraw, ImageFilter

        rng = random.Random(seed)
        dest.parent.mkdir(parents=True, exist_ok=True)

        img = Image.new("RGB", (width, height), (3, 4, 10))
        draw = ImageDraw.Draw(img)

        # Faint nebula wash so it is not a flat black rectangle.
        for _ in range(18):
            cx, cy = rng.randrange(width), rng.randrange(height)
            r = rng.randrange(int(width * 0.08), int(width * 0.30))
            tint = rng.choice([(18, 26, 60), (34, 18, 54), (12, 30, 48), (40, 24, 40)])
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=tint)
        img = img.filter(ImageFilter.GaussianBlur(radius=max(24, width // 60)))

        draw = ImageDraw.Draw(img)
        for _ in range(int(width * height / 5200)):
            x, y = rng.randrange(width), rng.randrange(height)
            b = rng.randint(110, 255)
            size = rng.choice([1, 1, 1, 2, 2, 3])
            draw.ellipse([x, y, x + size, y + size], fill=(b, b, min(255, b + 12)))

        # A handful of brighter stars with cross flares.
        for _ in range(max(6, width // 260)):
            x, y = rng.randrange(width), rng.randrange(height)
            arm = rng.randrange(6, 22)
            draw.line([x - arm, y, x + arm, y], fill=(210, 220, 255), width=1)
            draw.line([x, y - arm, x, y + arm], fill=(210, 220, 255), width=1)
            draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(255, 255, 255))

        img.save(str(dest), "PNG")
        log.info("Generated starfield fallback: %s (%dx%d)", dest.name, width, height)
        return Asset(
            path=dest, kind="image", title="Generated starfield",
            asset_id="generated", source="generated",
            credit="", width=width, height=height, query="fallback",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Starfield generation failed (%s).", exc)
        return None


# ---------------------------------------------------------------------------
# Dimension probing
# ---------------------------------------------------------------------------
def _probe_dimensions(path: Path, is_video: bool) -> tuple:
    """Return (width, height), or (0, 0) if it cannot be determined."""
    if is_video:
        try:
            import subprocess

            r = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height",
                 "-of", "csv=s=x:p=0", str(path)],
                capture_output=True, timeout=30,
            )
            txt = r.stdout.decode().strip().split("x")
            if len(txt) >= 2:
                return int(txt[0]), int(txt[1])
        except Exception:  # noqa: BLE001
            pass
        return 0, 0
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return 0, 0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def fetch_assets(queries: Sequence[str], want: Optional[int] = None,
                 dest_dir: Optional[Path] = None) -> List[Asset]:
    """Collect public-domain visuals for one video.

    Args:
        queries: search phrases from the topic's ``visual_queries``.
        want: how many assets to gather (defaults to nasa.max_assets_per_video).
        dest_dir: download directory.

    Returns:
        A list of Assets, possibly shorter than ``want``. Never raises — the
        caller is expected to handle an empty list by falling back to a
        generated background.
    """
    from modules.config import CACHE_DIR

    want = int(want or cfg("nasa.max_assets_per_video", 14))
    dest_dir = Path(dest_dir or CACHE_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)

    sources = [str(s).lower() for s in (cfg("nasa.sources", ["images_api"]) or [])]
    prefer_video = bool(cfg("nasa.prefer_video", True))
    queries = [q for q in queries if str(q).strip()] or ["space"]

    assets: List[Asset] = []
    seen_ids = set()

    # --- SVS first if enabled: native 4K animations are the best-looking source.
    if "svs" in sources:
        for q in queries[:3]:
            for cand in _search_svs(q, limit=4):
                if cand.asset_id and cand.asset_id not in seen_ids:
                    seen_ids.add(cand.asset_id)
        # NOTE: SVS entries are metadata-only here. Until its media URLs are
        # confirmed against a documented endpoint we do not download from it,
        # so nothing is appended. images-api below covers a lot of SVS material.

    # --- images-api.nasa.gov: the workhorse.
    if "images_api" in sources or not sources:
        # Interleave video and stills: motion holds attention, stills are sharper.
        plan: List[tuple] = []
        for q in queries:
            if prefer_video:
                plan.append((q, True))
            plan.append((q, False))

        for query, want_video in plan:
            if len(assets) >= want:
                break
            candidates = _search_images_api(query, want_video, limit=want)
            random.shuffle(candidates)
            for item in candidates:
                if len(assets) >= want:
                    break
                if item["nasa_id"] in seen_ids:
                    continue
                seen_ids.add(item["nasa_id"])
                got = _download_images_api_item(item, dest_dir, query)
                if got:
                    assets.append(got)
                    log.info("  [%d/%d] %s %s (%dx%d) — %s",
                             len(assets), want, got.kind, got.asset_id,
                             got.width, got.height, got.credit)

    # --- Pexels filler if NASA came up short.
    if len(assets) < max(3, want // 3):
        need = want - len(assets)
        log.info("Only %d NASA assets — topping up with Pexels (need %d).",
                 len(assets), need)
        for q in queries[:3]:
            if len(assets) >= want:
                break
            assets.extend(_fetch_pexels(q, dest_dir, want=need))

    if not assets:
        log.warning("No footage could be fetched — the renderer will fall back "
                    "to a generated starfield.")
    else:
        vids = sum(1 for a in assets if a.is_video)
        log.info("Fetched %d asset(s): %d video, %d still.",
                 len(assets), vids, len(assets) - vids)
    return assets


def attribution_lines(assets: Sequence[Asset]) -> List[str]:
    """Unique credit lines for the video description.

    NASA does not require attribution, but ESA/Hubble and ESO material is
    CC BY 4.0, where credit IS a licence condition. Crediting everything is
    simpler and safer than tracking which asset needs it.
    """
    seen, out = set(), []
    for a in assets:
        credit = (a.credit or "").strip()
        if credit and credit not in seen:
            seen.add(credit)
            out.append(credit)
    return out
