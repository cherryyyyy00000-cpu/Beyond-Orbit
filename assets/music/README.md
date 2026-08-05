# assets/music/ — optional ambient background music

Optional. Drop royalty-free ambient / drone / cinematic tracks here as `.mp3`,
`.m4a`, `.wav` or `.ogg` and one is picked at random per video.

Music is mixed **very low** — `video.music_volume` in `config.json` defaults to
`0.07` — so it sits underneath the narration rather than competing with it. The
whole mix is then loudness-normalised to about **-14 LUFS**, the YouTube
playback standard, so your videos are never quieter or louder than everyone
else's.

## Free sources that are safe to monetise

| Source | Notes |
|---|---|
| [Free Music Archive](https://freemusicarchive.org/) | Filter to CC0 / CC BY |
| [Pixabay Music](https://pixabay.com/music/) | Free for commercial use, no attribution |
| [Incompetech](https://incompetech.com/music/royalty-free/) | CC BY — credit required |
| [YouTube Audio Library](https://www.youtube.com/audiolibrary) | Free, inside Studio |
| [Freesound](https://freesound.org/) | Filter to CC0 |

If a track's licence requires attribution, add the credit line to
`channel.contact` in `config.json` or paste it into the video description —
`modules/metadata.py` already appends an attribution block.

> Leaving this folder empty is completely fine. Narration-only documentaries
> perform well, and it removes any music-licensing risk entirely.
