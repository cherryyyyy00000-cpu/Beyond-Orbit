"""Beyond Orbit — faceless space-documentary generator.

Module map
----------
config        Config + env loading, logging, runtime paths.
topic_source   Picks the next topic from topics/space_bank.json (no repeats).
script_writer  Turns a topic into a 7-layer-hook documentary script + chapters.
tts            edge-tts narration + word timings (free, no API key).
captions       .srt sidecar track + burned-in karaoke .ass for the Short.
nasa_fetch     Public-domain NASA SVS / images-api footage + stills.
video_builder  Renders the 16:9 long-form documentary (1440p default / 4K).
shorts_clipper Cuts the best ~40s out of the long-form into a vertical Short.
thumbnail      1280x720 thumbnails, 3 variants per video.
metadata       Titles, descriptions, chapters, hashtags, attribution block.
youtube        Resumable upload, private + publishAt scheduling, captions.
"""
