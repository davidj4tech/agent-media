"""The remote renderer's report has to reach us WHILE the audio is playing.

CLIP/DURATION are the only things that know how long a phone-lane utterance
is — nothing local measures audio rendered on another device — so if they
arrive late the progress row never carries a timeline and every display falls
back to polling the far side at ~2s a read. "Late" here meant *at process
exit*, i.e. after the utterance had finished, which is the same as never.
"""

from __future__ import annotations

import pytest

from agent_media_core.intake import submit


class _ReadAheadPipe:
    """A pipe that answers readline() but refuses to be iterated.

    Iterating a real pipe reads AHEAD: Python holds short lines in its buffer
    until the far side closes the stream, so `for line in proc.stdout` did not
    see these two lines until exit. A test can't easily reproduce that timing,
    so it forbids the construct outright — which is the property we want.
    """

    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        return self._lines.pop(0) if self._lines else b""

    def __iter__(self):
        raise AssertionError(
            "must not iterate the renderer's pipe — read-ahead withholds the "
            "report until EOF, by which point the utterance is over")


class _Proc:
    def __init__(self, lines):
        self.stdout = _ReadAheadPipe(lines)


class _State:
    def __init__(self):
        self.now_playing = None

    def set_now_playing(self, sink, **kw):
        self.now_playing = (sink, kw)


def _run(lines, report=None):
    state = _State()
    submit._watch_remote_progress(_Proc(lines), state, "phone", 1000.0, report)
    return state


def test_duration_reaches_the_row_without_iterating_the_pipe():
    state = _run([b"CLIP remote-123.mp3\n", b"DURATION 4.5\n"])
    assert state.now_playing is not None, "the progress row was never written"
    sink, kw = state.now_playing
    assert sink == "speech"
    extras = kw["extras"]
    assert extras["total_duration_s"] == 4.5
    assert extras["play_started_at"] > 0          # stamped on arrival, not submit
    assert extras["clip_uris"] == ["remote-123.mp3"]
    assert extras["clips_remote"] is True


def test_report_is_collected_for_history():
    report = {}
    _run([b"CLIP remote-9.mp3\n", b"DURATION 2.25\n"], report)
    assert report == {"clip": "remote-9.mp3", "duration": 2.25}


def test_duration_alone_is_enough():
    """A renderer that names no clip still gets a progress bar; it just isn't
    replayable from the far side."""
    state = _run([b"DURATION 8.0\n"])
    extras = state.now_playing[1]["extras"]
    assert extras["total_duration_s"] == 8.0
    assert "clip_uris" not in extras


def test_a_silent_renderer_writes_nothing():
    """Android TTS and a bare `say` measure nothing. A blank row is the correct
    answer then — better than inventing a length."""
    assert _run([]).now_playing is None


@pytest.mark.parametrize("junk", [b"DURATION\n", b"DURATION nope\n",
                                  b"DURATION 0\n", b"DURATION -3\n",
                                  b"hello\n"])
def test_junk_lines_never_produce_a_row(junk):
    assert _run([junk]).now_playing is None


def test_a_dead_pipe_is_not_an_error():
    """A progress bar must never be the reason an utterance fails."""

    class _Broken:
        stdout = None

    submit._watch_remote_progress(_Broken(), _State(), "phone", 1.0, {})


def test_progress_row_keeps_the_speaker_tags():
    """The DURATION rewrite must not drop the identity the first row carried —
    it replaces the row wholesale, so anything missing here is missing for the
    rest of the utterance (and the title falls back to the pane you're in)."""
    state = _State()
    submit._watch_remote_progress(
        _Proc([b"DURATION 3.0\n"]), state, "phone", 1000.0, None, None,
        {"source_pane": "%7", "source_window": "title-when-it-was-said"})
    extras = state.now_playing[1]["extras"]
    assert extras["source_pane"] == "%7"
    assert extras["source_window"] == "title-when-it-was-said"
