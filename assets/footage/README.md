# assets/footage/ — optional local B-roll

**You do not need to put anything here.** Beyond Orbit fetches its visuals
automatically from NASA on every run (`modules/nasa_fetch.py`), and falls back to
a generated starfield if the network fails. This folder is only for footage you
want to add yourself.

Drop `.mp4` / `.mov` / `.webm` files here and they join the rotation.

## Free, license-cleared sources

| Source | What you get | Licence |
|---|---|---|
| [NASA SVS](https://svs.gsfc.nasa.gov/) | 4K/UHD scientific animations — black holes, planetary flythroughs, solar activity | Public domain |
| [NASA Image and Video Library](https://images.nasa.gov/) | Huge mission archive, stills + video | Generally not copyrighted in the US |
| [ESA/Hubble](https://esahubble.org/images/) | Deep-space imagery | CC BY 4.0 — **credit required** |
| [Webb (STScI)](https://webbtelescope.org/images) | The sharpest infrared space images | Free with credit |
| [ESO](https://www.eso.org/public/images/) | Ground-based observatory imagery | CC BY 4.0 — **credit required** |
| [Pexels](https://www.pexels.com/) / [Pixabay](https://pixabay.com/) | Abstract space filler | Free, no attribution required |

## Two rules that will get you claimed or striked if you break them

1. **Never use the NASA logo, insignia, "meatball" or "worm" emblem.** NASA
   material is otherwise free to reuse, but its identifiers may not be used in a
   way that implies NASA endorses your channel. `modules/nasa_fetch.py` filters
   these out automatically — do not add them back by hand.
2. **Check for embedded third-party material.** Some NASA pages include licensed
   music or contractor-shot footage that NASA does not own. The fetcher skips
   assets whose metadata names a third-party copyright holder, but if you drop
   files in here manually, that check is on you.

Never rip footage from someone else's YouTube video. That is the fastest route
to a Content ID claim, and it takes your monetisation with it.

## Music

Put royalty-free ambient tracks in `assets/music/` instead. Volume is mixed very
low (`video.music_volume`, default `0.07`) so it sits under the narration.
