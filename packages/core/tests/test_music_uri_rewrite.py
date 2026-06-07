"""sink-music: YouTube → Mopidy-Mpv (mpv:) URI rewrite.

Shared YouTube links should play through the Mopidy-Mpv backend (robust
mpv+yt-dlp), while everything else stays on Mopidy's GStreamer path.
"""

import pytest

from agent_media_core.sinks.music import _to_music_uri
from agent_media_core.route.policy import detect_content_type, ContentType


@pytest.mark.parametrize("uri,want", [
    # YouTube in all the forms agent-media / shares produce -> mpv:
    ("yt:https://youtu.be/jNQXAC9IVRw", "mpv:https://youtu.be/jNQXAC9IVRw"),
    ("yt:https://www.youtube.com/watch?v=abc", "mpv:https://www.youtube.com/watch?v=abc"),
    ("https://youtu.be/jNQXAC9IVRw", "mpv:https://youtu.be/jNQXAC9IVRw"),
    ("https://www.youtube.com/watch?v=abc", "mpv:https://www.youtube.com/watch?v=abc"),
    ("youtube:video:abc123", "mpv:https://www.youtube.com/watch?v=abc123"),
    ("yt:abc123", "mpv:https://www.youtube.com/watch?v=abc123"),
    # Pass-through: already mpv, non-YouTube, library, playlists.
    ("mpv:https://youtu.be/x", "mpv:https://youtu.be/x"),
    ("https://example.com/stream.mp3", "https://example.com/stream.mp3"),
    ("local:track:Foo.flac", "local:track:Foo.flac"),
    ("spotify:track:xyz", "spotify:track:xyz"),
    ("yt:https://www.youtube.com/playlist?list=PL1",
     "yt:https://www.youtube.com/playlist?list=PL1"),
])
def test_to_music_uri(uri, want):
    assert _to_music_uri(uri) == want


def test_mpv_scheme_is_music_content_type():
    # No-intent ducking fallback must treat an mpv: URI as music.
    assert detect_content_type("mpv:https://youtu.be/x") is ContentType.MUSIC
