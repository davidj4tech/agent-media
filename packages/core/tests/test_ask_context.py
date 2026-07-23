"""Tests for the listening-context blurb (`media ask-context` / popup `a`).

`_ask_context(channel)` recombines what the popup already surfaces per channel
into one paste-ready line, which `media open-pi` prepends to the user's question
before opening a fresh pi window. It must never raise — a dead/idle backend
yields an empty (or partial) blurb, not a traceback in the popup's `a` path.
"""

import argparse

import pytest

from agent_media_core import cli


# --- music -----------------------------------------------------------------

def test_music_blurb_has_label_and_clock(monkeypatch):
    monkeypatch.setattr(cli, "SinkMusic", lambda: object())
    monkeypatch.setattr(
        cli, "_music_now_status",
        lambda m, w, hide_idle, bar: ("▶ 2:31 / 4:44", "Miles Davis — So What", ""))
    assert _ctx("music") == "I'm listening to music: Miles Davis — So What [2:31 / 4:44]"


def test_music_blurb_without_clock(monkeypatch):
    monkeypatch.setattr(cli, "SinkMusic", lambda: object())
    monkeypatch.setattr(
        cli, "_music_now_status",
        lambda m, w, hide_idle, bar: ("", "Some Track", ""))
    assert _ctx("music") == "I'm listening to music: Some Track"


def test_music_idle_is_empty(monkeypatch):
    monkeypatch.setattr(cli, "SinkMusic", lambda: object())
    monkeypatch.setattr(
        cli, "_music_now_status",
        lambda m, w, hide_idle, bar: ("○", "", ""))
    assert _ctx("music") == ""


def test_music_backend_error_is_empty(monkeypatch):
    def boom():
        raise OSError("mopidy down")
    monkeypatch.setattr(cli, "SinkMusic", boom)
    assert _ctx("music") == ""


# --- book ------------------------------------------------------------------

def _srv_with(np):
    class FakeSrv:
        def book_now_playing(self, target=""):
            return np
    return lambda: FakeSrv()


def test_book_blurb_title_chapter_clock(monkeypatch):
    monkeypatch.setattr(cli, "_srv", _srv_with({
        "idle": False, "title": "Dune", "chapter_title": "Chapter 3",
        "position_ms": 4_350_000, "duration_ms": 31_200_000,
    }))
    assert _ctx("book") == "I'm listening to an audiobook: Dune — Chapter 3 [1:12:30 / 8:40:00]"


def test_book_blurb_no_duration_shows_position_only(monkeypatch):
    monkeypatch.setattr(cli, "_srv", _srv_with({
        "idle": False, "title": "Dune", "chapter_title": "",
        "position_ms": 90_000, "duration_ms": 0,
    }))
    assert _ctx("book") == "I'm listening to an audiobook: Dune [1:30]"


def test_book_idle_is_empty(monkeypatch):
    monkeypatch.setattr(cli, "_srv", _srv_with({"idle": True}))
    assert _ctx("book") == ""


# --- speech ----------------------------------------------------------------

def test_speech_prefers_current_sentence(monkeypatch):
    monkeypatch.setattr(cli, "_now_speaking", lambda: {
        "extras": {"current_sentence": "The cat sat.", "text": "The cat sat. And more."}
    })
    assert _ctx("speech") == 'From the agent speech I\'m listening to: "The cat sat."'


def test_speech_falls_back_to_text(monkeypatch):
    monkeypatch.setattr(cli, "_now_speaking", lambda: {
        "extras": {"current_sentence": "", "text": "Full  turn\n text"}
    })
    assert _ctx("speech") == 'From the agent speech I\'m listening to: "Full turn text"'


def test_speech_idle_is_empty(monkeypatch):
    monkeypatch.setattr(cli, "_now_speaking", lambda: None)
    assert _ctx("speech") == ""


def test_default_channel_is_speech(monkeypatch):
    monkeypatch.setattr(cli, "_now_speaking", lambda: {
        "extras": {"current_sentence": "Hi.", "text": ""}})
    assert cli._ask_context("") == 'From the agent speech I\'m listening to: "Hi."'


# --- cmd_ask_context / cmd_open_pi -----------------------------------------

def test_cmd_ask_context_prints_blurb(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_ask_context", lambda ch: f"BLURB[{ch}]")
    assert cli.cmd_ask_context(argparse.Namespace(channel="music")) == 0
    assert capsys.readouterr().out.strip() == "BLURB[music]"


def test_open_pi_builds_prompt_and_opens_window(monkeypatch):
    monkeypatch.setattr(cli, "_ask_context", lambda ch: "CONTEXT")
    calls = {}

    def fake_run(argv, **kw):
        calls["argv"] = argv
        return argparse.Namespace(returncode=0)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.delenv("MEDIA_PI_CMD", raising=False)

    rc = cli.cmd_open_pi(argparse.Namespace(channel="book", question="who wrote this?"))
    assert rc == 0
    # new-window with a zsh -ic wrapper carrying the seeded p prompt.
    assert calls["argv"][:2] == ["tmux", "new-window"]
    inner = calls["argv"][2]
    assert inner.startswith("zsh -ic ")
    assert "CONTEXT" in inner and "who wrote this?" in inner
    assert "p " in inner


def test_open_pi_respects_media_pi_cmd(monkeypatch):
    monkeypatch.setattr(cli, "_ask_context", lambda ch: "")
    calls = {}
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda argv, **kw: calls.setdefault("argv", argv))
    monkeypatch.setenv("MEDIA_PI_CMD", "p -c")

    rc = cli.cmd_open_pi(argparse.Namespace(channel="speech", question="hey"))
    assert rc == 0
    assert "p -c " in calls["argv"][2]


def test_open_pi_empty_prompt_is_noop(monkeypatch):
    monkeypatch.setattr(cli, "_ask_context", lambda ch: "")
    called = {"run": False}
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: called.__setitem__("run", True))
    rc = cli.cmd_open_pi(argparse.Namespace(channel="music", question=""))
    assert rc == 1
    assert called["run"] is False


def _ctx(channel):
    return cli._ask_context(channel)
