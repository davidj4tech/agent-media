"""Sinks: where rendered audio actually plays.

`sink-speech` is mpv-backed (one broker, openal, XDG socket).
`sink-music` is Mopidy-backed (MPD client).
`sink-book` is mpv-backed (its own broker) — the longform book channel.

All implement the `Sink` protocol in `agent_media_core.types`.
"""

from .book import SinkBook
from .music import SinkMusic
from .speech import SinkSpeech

__all__ = ["SinkSpeech", "SinkMusic", "SinkBook"]
