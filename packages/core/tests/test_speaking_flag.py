"""The in-band "this loss is ours" flag.

The companion app holds audio focus on the music mpv's behalf and ducks when it
is lost. It must not duck for our own speech — the coordinator ducks that same
mpv, and two duckers on one volume lose the restore between them. The app cannot
work that out by watching playback: mpv takes the output when it *opens* a clip,
and a response is rendered and relayed ahead of time, so on 2026-08-14 the focus
loss arrived 37 s before the first clip was staged.

So the coordinator says so, on the socket the app already watches.
"""

import pytest

from agent_media_core.route import coordinator as coord_mod
from agent_media_core.sinks import speech as speech_mod
from agent_media_core.types import Target


@pytest.fixture(autouse=True)
def _quiet_env(monkeypatch):
    for var in ("MEDIA_DUCK_ROOMS_STREAM", "MEDIA_ANDROID_PAUSE_HOSTS",
                "MEDIA_MPRIS_HOSTS", "MEDIA_SPEECH_DEFAULT_TARGET"):
        monkeypatch.delenv(var, raising=False)


class _Recorder:
    """Stands in for the speech mpv; records what the flag writer sent."""

    def __init__(self):
        self.writes = []

    def __call__(self, sock, name, value, **kw):
        self.writes.append((str(sock), name, value))


def test_set_speaking_writes_the_observed_property(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(speech_mod.ipc, "set_property", rec)
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_PHONE", "tcp://127.0.0.1:6602")

    assert speech_mod.set_speaking(True, Target(name="phone")) is True
    assert rec.writes == [("tcp://127.0.0.1:6602",
                           "user-data/agent-media/speaking", True)]


def test_set_speaking_never_raises_at_the_caller(monkeypatch):
    """An mpv too old for user-data (< 0.36) errors, and a broker may be down.
    Neither is a reason to delay or drop a clip."""
    def boom(*a, **kw):
        raise speech_mod.ipc.MpvIpcError("property not found")

    monkeypatch.setattr(speech_mod.ipc, "set_property", boom)
    assert speech_mod.set_speaking(True) is False


def _coord(monkeypatch, rec, tmp_path):
    monkeypatch.setattr(speech_mod.ipc, "set_property", rec)

    class _SilentMusic:
        def now_playing_uri(self, target):
            return None

    from agent_media_core.state import StateStore

    c = coord_mod.Coordinator(music=_SilentMusic(),
                              state=StateStore(tmp_path / "state.db"))
    c._probe_book_active = lambda: False
    return c


def _flags(rec):
    """Just the flag values, in order, once the writer thread has drained."""
    return [value for _, name, value in rec.writes
            if name == speech_mod.SPEAKING_PROPERTY]


def test_a_response_raises_the_flag_and_lowers_it(monkeypatch, tmp_path):
    rec = _Recorder()
    c = _coord(monkeypatch, rec, tmp_path)

    c.before_speech()
    c.after_speech()
    c._flag_writer.shutdown(wait=True)

    assert _flags(rec) == [True, False]


def test_the_flag_goes_up_before_rendering_starts(monkeypatch, tmp_path):
    """pre_pause_remote runs ahead of render_text precisely because that work is
    slow — and it is during that work that the far mpv opens the clip and takes
    the output. Raising the flag only in before_speech would be too late."""
    rec = _Recorder()
    c = _coord(monkeypatch, rec, tmp_path)

    c.pre_pause_remote()
    c._flag_writer.shutdown(wait=True)

    assert _flags(rec) == [True]


def test_lowering_cannot_overtake_the_raise(monkeypatch, tmp_path):
    """Both writes are round trips to the phone, off the speech path. If they
    raced, a response could end with the flag stuck up — and a stuck flag means
    the app never ducks for anything."""
    order = []

    def slow(sock, name, value, **kw):
        if value:
            import time
            time.sleep(0.05)
        order.append(value)

    rec = _Recorder()
    c = _coord(monkeypatch, rec, tmp_path)
    monkeypatch.setattr(speech_mod.ipc, "set_property", slow)

    c._speaking(True)
    c._speaking(False)
    c._flag_writer.shutdown(wait=True)

    assert order == [True, False]


# ---- and what to call the reply --------------------------------------------
#
# The same socket, the same writer, the same fire-and-forget discipline. The
# phone's speech card and the car display both read `media-title`, and a
# rendered clip's is its own filename — so the card said "Sam", which is who is
# talking and not what about. David asked for the popup's title instead.


def _titles(rec):
    return [value for _, name, value in rec.writes
            if name == speech_mod.TITLE_PROPERTY]


def test_set_media_title_writes_the_override(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(speech_mod.ipc, "set_property", rec)
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_PHONE", "tcp://127.0.0.1:6602")

    assert speech_mod.set_media_title("companion focus", Target(name="phone")) is True
    assert rec.writes == [("tcp://127.0.0.1:6602",
                           "force-media-title", "companion focus")]


def test_an_empty_title_is_not_written(monkeypatch):
    """A reply with nothing to call itself must not clear the display into
    blankness — leaving the property alone keeps the app's own fallback in
    charge, which is a name rather than an empty line."""
    rec = _Recorder()
    monkeypatch.setattr(speech_mod.ipc, "set_property", rec)

    assert speech_mod.set_media_title("", Target(name="phone")) is False
    assert speech_mod.set_media_title("   ") is False
    assert rec.writes == []


def test_a_title_never_raises_at_the_caller(monkeypatch):
    def boom(*a, **kw):
        raise speech_mod.ipc.MpvIpcError("nope")

    monkeypatch.setattr(speech_mod.ipc, "set_property", boom)
    assert speech_mod.set_media_title("anything") is False


def test_before_speech_names_the_reply(monkeypatch, tmp_path):
    rec = _Recorder()
    c = _coord(monkeypatch, rec, tmp_path)

    c.before_speech(title="agent-media companion")
    c._flag_writer.shutdown(wait=True)

    assert _titles(rec) == ["agent-media companion"]
    # And the flag still goes up: the title rides alongside it, never instead.
    assert _flags(rec) == [True]


def test_an_unnamed_reply_writes_nothing(monkeypatch, tmp_path):
    """Every caller that has a title passes one; the ones that do not are not
    broken, and must not pay a round trip to say so."""
    rec = _Recorder()
    c = _coord(monkeypatch, rec, tmp_path)

    c.before_speech()
    c._flag_writer.shutdown(wait=True)

    assert _titles(rec) == []
