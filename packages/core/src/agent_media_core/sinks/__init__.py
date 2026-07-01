"""Sinks: where rendered audio actually plays.

`sink-speech` is mpv-backed (one broker, openal, XDG socket).
`sink-music` is Mopidy-backed (MPD client).
`sink-book` is mpv-backed (its own broker) — the longform book channel.

All implement the `Sink` protocol in `agent_media_core.types`.
"""

from typing import TYPE_CHECKING

from .music import SinkMusic
from .speech import SinkSpeech

if TYPE_CHECKING:  # for type-checkers only; not imported at runtime
    from .book import SinkBook

__all__ = ["SinkSpeech", "SinkMusic", "SinkBook"]


def __getattr__(name: str):
    # `SinkBook` pulls in mopidy → urllib (~24ms), which the hot control-surface
    # commands (status/now/speed/toggle via the CLI) never touch. Load it only
    # when actually asked for, so `from .sinks import _mpv_ipc`/SinkSpeech stays
    # cheap while `from .sinks import SinkBook` still works (PEP 562).
    if name == "SinkBook":
        from .book import SinkBook
        return SinkBook
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
