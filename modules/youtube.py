"""YouTube upload with scheduled publishing, captions and thumbnails.

The one thing most automated uploaders get wrong
-----------------------------------------------
YouTube takes a long time to finish processing a 1440p or 4K upload. If you post
publicly straight away, the video goes live while only the low-resolution
rendition exists — so the **first hour, which matters more to the algorithm than
any other**, is served at 360p to the viewers most likely to engage.

So this module never uploads straight to public. It uploads as **private with
``status.publishAt`` set**, which means:

    upload now (private)  ->  YouTube finishes HD processing  ->  auto-publishes
                              at exactly the intended peak time, in full quality

``youtube.upload_lead_hours`` controls that head start (default 5 hours).

Auth
----
Credentials are built from ``YT_REFRESH_TOKEN`` + ``YT_CLIENT_ID`` +
``YT_CLIENT_SECRET`` **first**, before any token file is considered. A stale
``youtube_token.json`` shadowing working secrets is a genuinely nasty failure
mode: uploads die with ``invalid_grant`` while the secrets look perfectly fine.

Scopes
------
    youtube.upload      required — publish videos
    youtube.force-ssl   needed for caption tracks and custom thumbnails

Public API
----------
    video_id = upload_video(path, title, description, tags, publish_at=...)
    upload_caption(video_id, srt_path)
    set_thumbnail(video_id, jpg_path)
    when = next_publish_time()
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import List, Optional, Sequence

from modules.config import cfg, get_env, setup_logging

log = setup_logging(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

_DAY_INDEX = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
def next_short_publish_times(count: int,
                             now: Optional[dt.datetime] = None,
                             first_immediate: bool = True) -> List[Optional[str]]:
    """Publish slots for the Shorts cut from one documentary.

    Shorts are all UPLOADED in the same run (that is where the API quota goes),
    but they should not all go live at once. This spreads them across the
    upcoming peak windows in ``shorts.publish_hours_et`` — US Shorts traffic
    peaks around lunchtime and again in the evening.

    An earlier version used ``now + i days + 1 hour``, which meant a 9 AM ET run
    published its Shorts at 10 AM ET — nowhere near a peak. This walks real
    clock slots instead.

    Args:
        count: how many slots are needed.
        now: override the clock (for testing).
        first_immediate: when True the first Short publishes right away. The
            daily Shorts run sets this False so all three land on real peak
            windows instead of one going out at whatever time the runner woke up.

    Returns a list of RFC3339 UTC strings; ``None`` means "publish immediately".
    """
    count = max(0, int(count))
    if count == 0:
        return []

    hours = cfg("shorts.publish_hours_et", [12, 19]) or [12, 19]
    try:
        from zoneinfo import ZoneInfo
        eastern = ZoneInfo("America/New_York")
    except Exception:  # noqa: BLE001
        return [None] * count

    now_et = (now or dt.datetime.now(dt.timezone.utc)).astimezone(eastern)
    # A 40-second vertical clip processes fast, so a short lead is enough.
    earliest = now_et + dt.timedelta(minutes=45)

    slots: List[Optional[str]] = [None] if first_immediate else []
    need = count - len(slots)
    for day_offset in range(0, 21):
        if need <= 0:
            break
        day = now_et.date() + dt.timedelta(days=day_offset)
        for hour in sorted(float(h) for h in hours):
            if need <= 0:
                break
            h, m = int(hour), int(round((hour - int(hour)) * 60))
            slot = dt.datetime.combine(day, dt.time(hour=h, minute=m), tzinfo=eastern)
            if slot < earliest:
                continue
            slots.append(slot.astimezone(dt.timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ"))
            need -= 1

    while len(slots) < count:      # ran out of slots — publish those now
        slots.append(None)

    log.info("Short publish slots: %s",
             ", ".join("now" if s is None else s for s in slots[:count]))
    return slots[:count]


def next_publish_time(now: Optional[dt.datetime] = None) -> Optional[str]:
    """Next publish slot as an RFC3339 UTC timestamp, or None if disabled.

    Slots are ``youtube.publish_hour_et`` on each ``youtube.publish_days``, in
    US Eastern time, and must be at least ``youtube.upload_lead_hours`` away so
    HD processing has time to finish.

    2 PM ET is deliberate: it catches the US after-school/after-work window, the
    7-11 PM evening peak, and the following morning — from one upload.
    """
    if not bool(cfg("youtube.schedule_publish", True)):
        return None

    try:
        from zoneinfo import ZoneInfo
        eastern = ZoneInfo("America/New_York")
    except Exception as exc:  # noqa: BLE001
        log.warning("Timezone data unavailable (%s) — publishing immediately.", exc)
        return None

    now_et = (now or dt.datetime.now(dt.timezone.utc)).astimezone(eastern)
    hour = int(cfg("youtube.publish_hour_et", 14))
    lead = float(cfg("youtube.upload_lead_hours", 5))
    days = [str(d).strip().lower() for d in (cfg("youtube.publish_days", []) or [])]
    wanted = {_DAY_INDEX[d] for d in days if d in _DAY_INDEX}
    if not wanted:
        wanted = set(range(7))

    earliest = now_et + dt.timedelta(hours=lead)
    for offset in range(0, 15):
        day = now_et.date() + dt.timedelta(days=offset)
        if day.weekday() not in wanted:
            continue
        slot = dt.datetime.combine(day, dt.time(hour=hour), tzinfo=eastern)
        if slot >= earliest:
            utc = slot.astimezone(dt.timezone.utc)
            log.info("Scheduled publish: %s ET (%s UTC), %.1f h from now.",
                     slot.strftime("%a %d %b %H:%M"),
                     utc.strftime("%Y-%m-%d %H:%M"),
                     (utc - now_et.astimezone(dt.timezone.utc)).total_seconds() / 3600)
            return utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    log.warning("Could not find a publish slot in the next two weeks.")
    return None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def _token_file() -> str:
    return get_env("YOUTUBE_TOKEN_FILE", "youtube_token.json") or "youtube_token.json"


def _client_secret_file() -> str:
    return get_env("YOUTUBE_CLIENT_SECRET_FILE", "client_secret.json") or "client_secret.json"


def get_credentials(interactive: bool = False):
    """Load OAuth credentials, preferring the three-secret path."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    refresh = get_env("YT_REFRESH_TOKEN")
    cid = get_env("YT_CLIENT_ID")
    csecret = get_env("YT_CLIENT_SECRET")

    if refresh and cid and csecret:
        log.info("Building credentials from the YT_REFRESH_TOKEN secret...")
        creds = Credentials(
            None, refresh_token=refresh,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=cid, client_secret=csecret, scopes=SCOPES,
        )
        creds.refresh(Request())
        return creds

    token_path = Path(_token_file())
    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read %s: %s", token_path, exc)

    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception as exc:  # noqa: BLE001
            log.warning("Stored token refresh failed (%s).", exc)

    if not interactive:
        raise RuntimeError(
            "No valid YouTube credentials. Set the YT_CLIENT_ID + "
            "YT_CLIENT_SECRET + YT_REFRESH_TOKEN secrets (get the refresh token "
            "from https://developers.google.com/oauthplayground with the "
            "youtube.upload and youtube.force-ssl scopes). Run "
            "`python verify_setup.py` to diagnose."
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    secret = _client_secret_file()
    if not Path(secret).exists():
        raise RuntimeError(f"OAuth client secret file not found: {secret}")
    flow = InstalledAppFlow.from_client_secrets_file(secret, SCOPES)
    creds = flow.run_local_server(port=0)
    Path(_token_file()).write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_service(interactive: bool = False):
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=get_credentials(interactive),
                 cache_discovery=False)


# ---------------------------------------------------------------------------
# Request body
# ---------------------------------------------------------------------------
def build_body(
    title: str,
    description: str,
    tags: Optional[Sequence[str]] = None,
    privacy: Optional[str] = None,
    publish_at: Optional[str] = None,
    category_id: Optional[str] = None,
) -> dict:
    """Assemble the videos.insert request body."""
    category_id = category_id or str(cfg("youtube.category_id", "28"))
    privacy = privacy or get_env("YOUTUBE_PRIVACY") or str(
        cfg("youtube.privacy_status", "public"))

    clean_tags: List[str] = []
    seen = set()
    for raw in (tags or []):
        t = str(raw).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            clean_tags.append(t)
    # YouTube caps the tag field at 500 characters in total, so trim to fit
    # rather than letting the API reject the whole upload.
    kept, used = [], 0
    for t in clean_tags:
        if used + len(t) + 1 > 480:
            break
        kept.append(t)
        used += len(t) + 1

    status = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": bool(cfg("youtube.made_for_kids", False)),
    }
    if publish_at:
        # Scheduling REQUIRES privacyStatus 'private' alongside publishAt.
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at

    return {
        "snippet": {
            "title": str(title).strip()[:100],
            "description": str(description).strip()[:5000],
            "tags": kept,
            "categoryId": category_id,
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
        },
        "status": status,
    }


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
def upload_video(
    video_path,
    title: str,
    description: str,
    tags: Optional[Sequence[str]] = None,
    privacy: Optional[str] = None,
    publish_at: Optional[str] = None,
    thumbnail_path: Optional[Path] = None,
    caption_path: Optional[Path] = None,
    service=None,
    interactive: bool = False,
) -> Optional[str]:
    """Upload one video. Returns the video id, or None on failure."""
    from googleapiclient.http import MediaFileUpload

    video_path = Path(video_path)
    if not video_path.exists():
        log.error("Video not found: %s", video_path)
        return None

    size_mb = video_path.stat().st_size / 1_048_576
    if service is None:
        try:
            service = build_service(interactive=interactive)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not build the YouTube service: %s", exc)
            return None

    body = build_body(title, description, tags, privacy, publish_at)

    # Resumable upload with a bounded chunk size. chunksize=-1 sends the file in
    # one request, which is fine for a Short but fails on a multi-gigabyte 4K
    # documentary — an interrupted single-shot upload has to start over.
    media = MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024,
                            resumable=True, mimetype="video/mp4")

    log.info("Uploading %r (%.0f MB, privacy=%s%s)",
             body["snippet"]["title"], size_mb, body["status"]["privacyStatus"],
             f", publishAt={publish_at}" if publish_at else "")

    try:
        request = service.videos().insert(part="snippet,status", body=body,
                                          media_body=media)
        response = None
        last_pct = -10
        while response is None:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                if pct - last_pct >= 10:
                    log.info("  upload %d%%", pct)
                    last_pct = pct
        video_id = response.get("id")
        if not video_id:
            log.error("Upload returned no video id: %s", response)
            return None
        log.info("Uploaded: https://youtu.be/%s", video_id)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        log.error("Upload failed: %s", msg[:400])
        if "quotaExceeded" in msg:
            log.error("Daily API quota is exhausted (an upload costs 1,600 of "
                      "10,000 units). It resets at midnight Pacific time.")
        elif "uploadLimitExceeded" in msg:
            log.error("This channel has hit its daily upload limit.")
        return None

    # Extras are best-effort: never fail a successful upload over them.
    if thumbnail_path:
        set_thumbnail(video_id, thumbnail_path, service=service)
    if caption_path:
        # Derive the BCP-47 code from channel.language, e.g. "English (US)" -> "en".
        lang = str(cfg("channel.language", "English (US)")).strip().lower()
        code = "en" if lang.startswith("english") else (lang[:2] or "en")
        upload_caption(video_id, caption_path, language=code, service=service)
    return video_id


def set_thumbnail(video_id: str, thumbnail_path, service=None) -> bool:
    """Attach a custom thumbnail. Requires a verified channel."""
    from googleapiclient.http import MediaFileUpload

    thumbnail_path = Path(thumbnail_path)
    if not thumbnail_path.exists():
        log.warning("Thumbnail file missing: %s", thumbnail_path)
        return False
    if thumbnail_path.stat().st_size > 2 * 1024 * 1024:
        log.warning("Thumbnail exceeds YouTube's 2 MB limit — skipping.")
        return False
    try:
        service = service or build_service()
        service.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumbnail_path)),
        ).execute()
        log.info("Custom thumbnail set.")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not set the thumbnail: %s", str(exc)[:220])
        log.warning("Custom thumbnails need a verified channel (Studio -> "
                    "Settings -> Channel -> Feature eligibility) and the "
                    "youtube.force-ssl scope.")
        return False


def upload_caption(video_id: str, srt_path, language: str = "en",
                   name: str = "English", service=None) -> bool:
    """Upload an .srt caption track.

    Captions are worth the extra call: YouTube indexes the text for search, and
    a large share of viewers watch with captions on.
    """
    from googleapiclient.http import MediaFileUpload

    srt_path = Path(srt_path)
    if not srt_path.exists():
        log.warning("Caption file missing: %s", srt_path)
        return False
    try:
        service = service or build_service()
        service.captions().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "language": language,
                    "name": name,
                    "isDraft": False,
                }
            },
            media_body=MediaFileUpload(str(srt_path), mimetype="application/octet-stream"),
        ).execute()
        log.info("Caption track uploaded (%s).", language)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not upload captions: %s", str(exc)[:220])
        log.warning("captions.insert needs the youtube.force-ssl scope — if the "
                    "refresh token was created without it, redo the OAuth "
                    "Playground step with both scopes.")
        return False


def ensure_playlist(title: str, description: str = "", service=None) -> Optional[str]:
    """Find or create a playlist, returning its id.

    Playlists are the cheapest watch-time lever available. A viewer who finishes a
    video inside a playlist is auto-advanced to the next one, so session time
    compounds instead of ending — and session time is what the algorithm actually
    rewards. Beyond Orbit had none.

    Costs 1 unit to look up and 50 to create, against a 10,000 daily budget.
    """
    if not title:
        return None
    try:
        service = service or build_service()
        # Look for it first so a run never creates duplicates.
        req = service.playlists().list(part="snippet", mine=True, maxResults=50)
        while req is not None:
            resp = req.execute()
            for item in resp.get("items", []):
                if item["snippet"]["title"].strip().lower() == title.strip().lower():
                    return item["id"]
            req = service.playlists().list_next(req, resp)

        created = service.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {"title": title[:150],
                            "description": description[:5000],
                            "defaultLanguage": "en"},
                "status": {"privacyStatus": "public"},
            },
        ).execute()
        log.info("Created playlist %r (%s)", title, created["id"])
        return created["id"]
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not find or create the playlist %r: %s",
                    title, str(exc)[:200])
        log.warning("Playlist operations need the youtube.force-ssl scope.")
        return None


def add_to_playlist(playlist_id: str, video_id: str, service=None) -> bool:
    """Append a video to a playlist. Best-effort; never fails an upload."""
    if not (playlist_id and video_id):
        return False
    try:
        service = service or build_service()
        service.playlistItems().insert(
            part="snippet",
            body={"snippet": {"playlistId": playlist_id,
                              "resourceId": {"kind": "youtube#video",
                                             "videoId": video_id}}},
        ).execute()
        log.info("Added %s to playlist %s", video_id, playlist_id)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not add %s to the playlist: %s", video_id, str(exc)[:200])
        return False


def get_my_channel(service=None) -> Optional[dict]:
    """Return the channel this token controls — used by verify_setup.py."""
    try:
        service = service or build_service()
        resp = service.channels().list(part="snippet,statistics", mine=True).execute()
        items = resp.get("items") or []
        return items[0] if items else None
    except Exception as exc:  # noqa: BLE001
        log.error("Could not read the channel: %s", str(exc)[:200])
        return None
