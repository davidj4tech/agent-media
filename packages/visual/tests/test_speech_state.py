"""The canvas's speech-state snapshot (drives motion + sound cues)."""

import json
from agent_media_visual import canvas


def _fake_media(monkeypatch, line):
    monkeypatch.setattr(canvas, "_media", lambda args, timeout=10: line)


def test_playing_with_clock(monkeypatch):
    _fake_media(monkeypatch, "▶ 00:12 / 02:05")
    st = canvas.speech_state()
    assert st == {"kind": "state", "speaking": True, "pos": 12, "dur": 125}


def test_paused_is_not_speaking(monkeypatch):
    _fake_media(monkeypatch, "⏸ 00:12 / 02:05")
    st = canvas.speech_state()
    assert st["speaking"] is False
    assert st["pos"] == 12


def test_idle(monkeypatch):
    _fake_media(monkeypatch, "○")
    st = canvas.speech_state()
    assert st == {"kind": "state", "speaking": False}


def test_hour_long_clock(monkeypatch):
    _fake_media(monkeypatch, "▶ 1:02:03 / 1:10:00")
    st = canvas.speech_state()
    assert st["pos"] == 3723 and st["dur"] == 4200


def test_empty_output(monkeypatch):
    _fake_media(monkeypatch, "")
    st = canvas.speech_state()
    assert st == {"kind": "state", "speaking": False}


def test_state_events_do_not_clobber_last_image():
    hub = canvas.Hub()
    hub.publish({"image": "/img/x.webp"})
    hub.publish({"kind": "state", "speaking": True})
    assert hub.last == {"image": "/img/x.webp"}
    assert hub.last_state == {"kind": "state", "speaking": True}


def test_state_carries_sentence_and_visual_flag(monkeypatch):
    _fake_media(monkeypatch, "▶ 00:02 / 00:30")
    monkeypatch.setattr(canvas, "_speech_extras",
                        lambda: {"current_sentence": "  The   very sentence. ",
                                 "visual": "figure"})
    st = canvas.speech_state()
    assert st["sentence"] == "The very sentence."
    assert st["visual"] == "figure"


def test_idle_state_reads_extras_for_the_transcript(monkeypatch):
    """An idle state carries the last clip's LINES, but not its live fields.

    This asserted the opposite until 2026-08-20: that an idle poll never
    touched the store at all. That was a fair guarantee when the only thing
    extras were wanted for was the live sentence and the figure flag — neither
    of which means anything with nothing playing, so the read was pure waste at
    1 Hz.

    The canvas transcript changed what extras are for. clip_sentences is the
    LAST reply's until a new one replaces it, and a transcript is wanted
    precisely when the voice has stopped — so refusing to read while idle meant
    the transcript was empty every single time anybody went looking for it. The
    read is a cheap WAL hit; an unreachable feature is not a saving.

    What must still hold is that an idle frame does not claim a voice: no
    `sentence`, no `visual`.
    """
    _fake_media(monkeypatch, "○")
    monkeypatch.setattr(canvas, "_speech_extras",
                        lambda: {"current_sentence": "Said a moment ago.",
                                 "visual": "figure",
                                 "clip_sentences": ["Said a moment ago.",
                                                    "And then this."],
                                 "current_sentence_idx": 1})
    st = canvas.speech_state()
    assert st["speaking"] is False
    assert "sentence" not in st and "visual" not in st
    assert st["lines"] == ["Said a moment ago.", "And then this."]
    assert st["lidx"] == 1


def test_render_only_host_asks_the_origin(monkeypatch):
    """A host that only PLAYS the speech gets the words from the one producing it.

    The phone's local canvas has no now_playing for speech — it is written
    where the reply is produced, not where the audio comes out — so its
    subtitle, band, seam and transcript were all permanently empty. Not broken:
    asking a store that was never going to have the answer. From the outside
    that is indistinguishable from a canvas nobody deployed, which is exactly
    how it was read for six rounds.
    """
    monkeypatch.setattr(canvas, "_origin_host", lambda: "red5")
    monkeypatch.setattr(canvas, "_ORIGIN_STATE", {"t": 0.0, "data": None})
    monkeypatch.setattr(canvas, "_speech_extras",
                        lambda: (_ for _ in ()).throw(AssertionError("local read")))

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"kind": "state", "speaking": True,
                               "sentence": "From the origin.",
                               "lines": ["From the origin.", "And the rest."],
                               "lidx": 0, "events": [1], "local_audio": True}).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    st = canvas.speech_state()
    assert st["lines"] == ["From the origin.", "And the rest."]
    assert st["sentence"] == "From the origin."
    # The peek-only fields do not ride the 1 Hz broadcast to every screen.
    assert "events" not in st and "local_audio" not in st


def test_origin_unreachable_keeps_the_last_words(monkeypatch):
    """One dropped packet must not blink the band out of existence."""
    monkeypatch.setattr(canvas, "_origin_host", lambda: "red5")
    good = {"kind": "state", "speaking": True, "lines": ["Held."], "lidx": 0}
    monkeypatch.setattr(canvas, "_ORIGIN_STATE", {"t": 0.0, "data": good})

    def _boom(*a, **k):
        raise OSError("link dropped")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    assert canvas.speech_state()["lines"] == ["Held."]
