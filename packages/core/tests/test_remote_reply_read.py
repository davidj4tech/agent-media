"""A reply ends the reply being read at the end of its sentence — on the phone.

The phone plays a reply's clips as one list, by itself, so there is no gap
between sentences where the follow loop gets to decide. A `read` cut (the
listener replied, `session_reply_read`) is noted with the sentence playing,
and the player is stopped when it moves past it — not on the tick that saw
the cut, which would clip the listener's current sentence mid-word.
"""

from pathlib import Path

from agent_media_core.intake import submit as S
from agent_media_core.state import StateStore
from agent_media_core.types import Event, Source, Target

from test_remote_playlist_hold import _RecordingCoord, state_env  # noqa: F401

A = "sess-a"


def _render(text, outfile, **_):
    Path(outfile).write_bytes(b"\x00")
    return True, ""


class _PhoneSink:
    """On the first sentence for a while (the reply is read three ticks in),
    then the second, then the third. Records the position of every stop."""

    def __init__(self):
        self.ticks = 0
        self.stops: list[int] = []
        self.pos = 0

    def prefetch(self, paths, target=None): pass
    def play_playlist(self, uris, target=None, gapless=True): pass
    def set_playlist_pos(self, pos, target=None): pass
    def muted(self, target): return False
    def idle(self, target): return not self.stops

    def stop(self, target=None):
        self.stops.append(self.pos)

    def snapshot(self, target=None):
        self.ticks += 1
        if self.stops:
            return {"idle-active": True, "pause": False, "playlist-count": 0}
        if self.ticks == 3:
            S.session_reply_read(A)
        self.pos = 0 if self.ticks < 8 else 1 if self.ticks < 12 else 2
        return {"idle-active": False, "pause": False, "playlist-pos": self.pos,
                "time-pos": self.ticks, "playlist-count": 3}


def test_the_phone_finishes_the_sentence_then_stops(state_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(S, "render_text", _render)
    sink = _PhoneSink()
    S.submit_event(Event(text="First sentence here. Second sentence here. Third one too.",
                         source=Source.CLAUDE_CODE, target=Target(name="phone"),
                         metadata={"session": A}),
                   state=StateStore(), sink=sink, coordinator=_RecordingCoord())
    assert sink.stops, "the reply played on after it was answered"
    assert sink.stops[0] == 1, (
        f"stopped on sentence {sink.stops[0] + 1}: it should finish the first "
        "(the one the reply landed on) and stop as the second begins")
    assert sink.ticks >= 8, "stopped mid-sentence, on the tick that saw the reply"
