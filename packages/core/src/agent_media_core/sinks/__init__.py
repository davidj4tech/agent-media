"""Sinks: where rendered audio actually plays.

`sink-speech` is mpv-backed (one broker, openal, XDG socket).
`sink-music` is Mopidy-backed (MPD client).

Both implement the `Sink` protocol in `agent_media_core.types`.
"""

from .music import SinkMusic
from .speech import SinkSpeech

__all__ = ["SinkSpeech", "SinkMusic"]
