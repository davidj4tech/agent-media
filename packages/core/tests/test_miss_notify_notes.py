"""The missed-speech note goes down the phone's own stream (roadmap item 15, #5).

`_try_notify` used to be `ssh <phone> termux-notification`. Now the canvas
takes the note (`phone_notes`, server notes.py) and holds it for a phone that
is asleep; a try counts as delivered only while a phone is listening, so the
retrier keeps the ledger (and `media status`'s alert) until one is. ssh stays
only for an app that has never shown notes.
"""

import subprocess

import pytest

from agent_media_core import phone_notes
from agent_media_core.sinks import _miss_notify


@pytest.fixture
def ssh(monkeypatch):
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def _answer(monkeypatch, **answer):
    posted = []

    def fake(route, body, timeout):
        posted.append((route, body))
        return {"ok": True, **answer} if answer else None

    monkeypatch.setattr(phone_notes, "_call", fake)
    return posted


def test_a_listening_phone_gets_the_note_and_no_ssh(monkeypatch, ssh):
    posted = _answer(monkeypatch, listening=1, seen=True)
    assert _miss_notify._try_notify("p8a", 2, 1_790_000_000) is True
    (route, body), = posted
    assert route == "/notes" and body["id"] == "speech-miss" and body["keep"] is True
    assert "2 spoken replies" in body["text"]
    assert ssh == []


def test_an_asleep_phone_is_not_delivered_yet(monkeypatch, ssh):
    _answer(monkeypatch, listening=0, seen=True)
    assert _miss_notify._try_notify("p8a", 1, 1_790_000_000) is False
    assert ssh == []


@pytest.mark.parametrize("answer", [{"listening": 0, "seen": False}, {}])
def test_an_app_without_notes_or_no_canvas_falls_back_to_ssh(monkeypatch, ssh, answer):
    _answer(monkeypatch, **answer)
    assert _miss_notify._try_notify("p8a", 1, 1_790_000_000) is True
    assert ssh and ssh[0][0] == "ssh" and "termux-notification" in ssh[0][-1]


def test_switched_off_goes_straight_to_ssh(monkeypatch, ssh):
    posted = _answer(monkeypatch, listening=1, seen=True)
    monkeypatch.setenv("MEDIA_PHONE_NOTES", "0")
    assert _miss_notify._try_notify("p8a", 1, 1_790_000_000) is True
    assert posted == [] and ssh[0][0] == "ssh"
