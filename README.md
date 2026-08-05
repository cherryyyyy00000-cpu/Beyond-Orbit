# Beyond Orbit

Automated **faceless space documentaries** for YouTube. One GitHub Actions run
picks a topic, writes a hook-structured script, narrates it, pulls real
public-domain NASA imagery, renders a 1440p documentary, cuts three vertical
Shorts out of it, and schedules everything to publish at US peak time.

No filming, no paid APIs, no server.

---

## Why long-form, and why that decision drives everything here

YouTube confirmed that **watch time from the Shorts feed does not count toward
the 4,000-hour Partner Program threshold**
([YPP eligibility](https://support.google.com/youtube/answer/72851)). Long-form
watch time does.

That single fact splits the work in two:

| | Job | Counts toward |
|---|---|---|
| **Documentary** (12 min, 16:9) | the watch-hours engine | the 4,000-hour path |
| **Shorts** (3 x 40s, 9:16) | the discovery engine — brings subscribers | the 10M-Shorts path |

The maths is stark. The Shorts route needs 10 million views in a **rolling** 90
days — about 111,000 views a day, sustained, because older views keep dropping
out of the window. The long-form route needs 4,000 hours in a rolling **12**
months, which at a 12-minute average is roughly **55 views a day**.

So the documentary is the product, and the Shorts are the funnel. Both come from
one render.

---

## What it actually costs

Nothing.

| Stage | Tool | Free? |
|---|---|---|
| Topics | 50 hand-written briefs in `topics/space_bank.json` | yes |
| Script | Gemini free tier (falls back to offline templates) | yes |
| Narration | [edge-tts](https://github.com/rany2/edge-tts) — Microsoft neural voices, **no API key** | yes |
| Footage | [NASA Image and Video Library](https://images.nasa.gov/) — **no API key** | yes |
| Captions | word timings from edge-tts -> `.srt` + karaoke `.ass` | yes |
| Render | `ffmpeg` | yes |
| Thumbnails | `ffmpeg` frame grab + Pillow | yes |
| Upload | YouTube Data API v3 free quota | yes |
| Scheduling | GitHub Actions free tier | yes |

**NASA content is generally not subject to copyright in the United States**
([NASA media guidelines](https://www.nasa.gov/nasa-brand-center/images-and-media/)),
which is this channel's real advantage: broadcast-quality space imagery with
effectively zero Content ID risk.

---

## Setup

### 1. YouTube credentials

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com).
2. Enable **YouTube Data API v3** in *APIs & Services -> Library*.
3. *OAuth consent screen* -> **External**. Then **PUBLISH APP** so it is
   "In production" — while it sits in *Testing*, Google expires the refresh token
   after **7 days** and the pipeline breaks every week.
4. *Credentials* -> **Create OAuth client ID** -> **Web application**, and add
   this authorized redirect URI exactly (no trailing slash):
   ```
   https://developers.google.com/oauthplayground
   ```
5. Get a refresh token at
   [OAuth Playground](https://developers.google.com/oauthplayground):
   gear icon -> *Use your own OAuth credentials* -> paste your client ID/secret,
   then request **both** of these scopes:
   ```
   https://www.googleapis.com/auth/youtube.upload
   https://www.googleapis.com/auth/youtube.force-ssl
   ```
   `force-ssl` is what allows the caption track and the custom thumbnail. Upload
   works without it; captions and thumbnails do not.

> **In the account chooser, pick the Beyond Orbit channel.** Choosing the wrong
> one is the single most common setup mistake — every upload then silently lands
> on that other channel. `verify_setup.py` reports which channel the token
> actually controls, so run it before trusting anything.

### 2. Gemini (effectively required)

Get a free key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

**Be aware of what happens without it.** `topics/space_bank.json` holds a research
*brief* — a hook, a promise and a set of beats — not a finished script. Gemini
expands that into 1,500-2,200 words (~12 minutes). The offline template mode only
reaches **2-3 minutes**, because padding terse beats out to 1,500 words locally
would be pure filler, and filler both wrecks retention and is exactly what
YouTube's inauthentic-content policy targets.

So `script.min_publishable_minutes` (default 6) acts as a gate: a short script is
**skipped**, the topic stays in the rotation, and nothing is published. Set
`script.allow_short_publish: true` if you genuinely want short videos out.

### 3. Repository secrets

*Settings -> Secrets and variables -> Actions*:

| Secret | Required | Notes |
|---|---|---|
| `YT_CLIENT_ID` | yes | |
| `YT_CLIENT_SECRET` | yes | |
| `YT_REFRESH_TOKEN` | yes | for the **Beyond Orbit** channel |
| `GEMINI_API_KEY` | in practice yes | without it every topic is skipped |
| `PEXELS_API_KEY` | no | filler B-roll only |

> Keep the repository **public** for unlimited Actions minutes (private repos get
> 2,000/month). Secrets are never stored in the repo, so this is safe.

### 4. Check it

```bash
pip install -r requirements.txt
python verify_setup.py
```

This tests the live API rather than your assumptions: it refreshes the token,
prints the granted scopes, and — most importantly — names **which channel** the
token controls and compares it with `config.json`.

---

## Running it

```bash
python generate.py                 # build one documentary + its Shorts
python generate.py --topic t007    # a specific topic
python generate.py --dry-run       # script only, no render
python upload_youtube.py           # upload what is pending
python upload_youtube.py --now     # publish immediately instead of scheduling
python finalize_rotation.py        # mark topics used (only ones that uploaded)
```

System packages: `sudo apt-get install -y ffmpeg fonts-dejavu-core`.

### Automation

`.github/workflows/documentary.yml` runs at ~9 AM ET on **Thursday, Friday and
Saturday**, and there is a manual **Run workflow** button with a preview mode
that builds a downloadable artifact without posting anything.

---

## Two design decisions worth knowing about

### Upload private, publish later

YouTube takes a long time to process a 1440p or 4K upload. Post publicly right
away and the video goes live while only the low-resolution rendition exists — so
its **first hour, the hour that matters most to the algorithm**, is served at
360p.

So nothing is uploaded straight to public. The file goes up as **private with
`status.publishAt`** set, five hours ahead (`youtube.upload_lead_hours`):

```
09:00 ET  upload (private)  ->  YouTube finishes HD processing
14:00 ET  auto-publishes at full quality, at peak time
```

2 PM ET catches the US after-school/after-work window, the 7-11 PM evening peak,
and the following morning, from one upload.

### The render is three passes, and the video is encoded once

A 12-minute film with a visual change every 4-8 seconds is ~120 shots. Putting
120 inputs into one `-filter_complex` produces a command line tens of kilobytes
long and a process that needs more memory than a free runner has. Instead:

```
Pass 1   encode each shot to a normalised segment   <- the only encode
Pass 2   concat the segments with -c copy           <- no re-encode
Pass 3   mux narration + music with -c:v copy       <- no re-encode
```

That matters when you have no GPU and a 6-hour job ceiling.

---

## Resolution: why 1440p is the default

YouTube encodes **1440p and above with VP9**, which looks better than a 1080p
upload *even for viewers watching at 1080p*. It also costs about a third of 4K's
encode time.

| Setting | Encode time (12 min, no GPU) | Verdict |
|---|---|---|
| `1080p` | ~15 min | fine, but leaves quality on the table |
| **`1440p`** | ~20-35 min | **default** |
| `4k` + `preset: fast` | ~45-90 min | works |
| `4k` + `preset: medium` | 2-6 hours | risks the 6-hour job limit |

Change `video.resolution` in `config.json`, or set `BO_RESOLUTION` for one run.

---

## Layout

```
config.json              every tunable, with the reasoning in "comment" fields
topics/space_bank.json   50 topic briefs (hook, promise, beats, visuals, tags)
verify_setup.py          pre-flight check — run this first
generate.py              build a documentary + Shorts
upload_youtube.py        upload and schedule
finalize_rotation.py     mark topics used, only after a confirmed upload
modules/
  config.py              config, env, logging, resolution
  topic_source.py        topic rotation
  script_writer.py       7-layer hook structure, chapters
  tts.py                 edge-tts narration, chunked + retried
  captions.py            .srt track + karaoke .ass
  nasa_fetch.py          public-domain footage, with licence filters
  video_builder.py       the 16:9 renderer
  shorts_clipper.py      picks the best non-overlapping windows
  thumbnail.py           1280x720, three variants
  metadata.py            titles, description, chapters, attribution
  youtube.py             resumable upload, publishAt, captions, thumbnails
```

`config.json` is the place to make changes; every section carries a `comment`
explaining why the defaults are what they are.

---

## Licensing rules enforced in code

`modules/nasa_fetch.py` refuses two categories of asset outright:

1. **The NASA logo, insignia, "meatball" and "worm".** NASA material is otherwise
   free to reuse, but its identifiers may not be used in a way that implies NASA
   endorses this channel. Matching is word-boundary based, so *wormhole* and
   *sealed* are not false positives.
2. **Anything whose metadata names a third-party rights holder.** Some NASA pages
   embed licensed music or contractor footage NASA does not own.

ESA/Hubble and ESO imagery is CC BY 4.0, where credit is a licence condition, so
`modules/metadata.py` appends an attribution block to every description along
with an explicit statement that the channel is not affiliated with NASA or ESA.

---

## Honest limitations

- **Gemini is effectively required.** Without a key, scripts are 2-3 minutes and
  the publishable gate skips every topic. This is deliberate, not a bug.
- **The topic bank is finite.** 50 topics at 3 uploads a week is about four
  months. `finalize_rotation.py` warns when 8 or fewer remain, and refuses to
  loop — republishing the same 50 topics is what the inauthentic-content policy
  penalises.
- **NASA search quality varies.** Some topics return fewer good assets than
  others. The fetcher tops up from Pexels when a key is present, and falls back to
  a generated starfield so a render never fails.
- **Nobody can guarantee virality.** This builds the machine — hooks, retention
  structure, thumbnails, timing, SEO, HD quality. The first 10-20 videos are data
  collection; double down on whatever works.

---

## Disclaimer

For personal content-creation use. You are responsible for complying with the
YouTube and Google Terms of Service, and for the licensing of any footage or music
you add yourself. Not affiliated with, endorsed by, or sponsored by NASA or ESA.
