"""Tests for the media CLI pure helpers + parser (task #9, PR A)."""

from agent_media_core import cli


def test_fmt_mmss():
    assert cli.fmt_mmss(None) == "--:--"
    assert cli.fmt_mmss(0) == "00:00"
    assert cli.fmt_mmss(8) == "00:08"
    assert cli.fmt_mmss(75) == "01:15"
    assert cli.fmt_mmss(-3) == "00:00"


def test_progress_bar():
    assert cli.progress_bar(0.0, 10) == "░" * 10
    assert cli.progress_bar(1.0, 10) == "█" * 10
    assert cli.progress_bar(0.5, 10) == "█████░░░░░"
    assert cli.progress_bar(2.0, 4) == "████"   # clamped


def test_render_status_idle():
    assert cli.render_status(idle=True, pos=0, dur=0, paused=False,
                             muted=False) == ""
    assert cli.render_status(idle=None, pos=None, dur=None, paused=None,
                             muted=None) == ""
    assert cli.render_status(idle=True, pos=0, dur=0, paused=False,
                             muted=False, hide_idle=False) == "○"


def test_render_status_playing():
    s = cli.render_status(idle=False, pos=8, dur=55, paused=False,
                          muted=False, width=12)
    assert s.startswith("▶ 00:08 ")
    assert s.endswith(" 00:55")
    assert "█" in s and "░" in s


def test_render_status_paused_and_muted():
    s = cli.render_status(idle=False, pos=10, dur=20, paused=True, muted=True)
    assert s.startswith("⏸ ")
    assert s.endswith("[M]")


def test_parser_has_all_subcommands():
    p = cli._build_parser()
    # argparse stores subcommand names on the subparsers action choices
    sub = next(a for a in p._actions if a.choices and "status" in a.choices)
    for name in ("status", "now", "toggle", "pause", "resume", "stop", "mute",
                 "seek", "volume", "speed", "replay", "history", "say", "music"):
        assert name in sub.choices, name


def test_replay_resolves_history(monkeypatch):
    played = {}

    class FakeStore:
        def recent_history(self, *, sink, limit):
            return [{"uri": "/clips/latest.mp3", "text": "a"},
                    {"uri": "/clips/older.mp3", "text": "b"}][:limit]

    class FakeSpeech:
        def play(self, uri, target):
            played["uri"] = uri

    monkeypatch.setattr(cli, "StateStore", FakeStore)
    monkeypatch.setattr(cli, "SinkSpeech", lambda: FakeSpeech())

    class A:
        index = 1
    assert cli.cmd_replay(A()) == 0
    assert played["uri"] == "/clips/latest.mp3"

    class A2:
        index = 2
    assert cli.cmd_replay(A2()) == 0
    assert played["uri"] == "/clips/older.mp3"
