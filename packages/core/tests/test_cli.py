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


def test_render_status_no_bar():
    line = cli.render_status(idle=False, pos=30, dur=120, paused=False,
                             muted=False, bar=False)
    assert line == "▶ 00:30 / 02:00"
    assert "█" not in line and "░" not in line


def test_parse_kv_music():
    from agent_media_core.sinks.music import _parse_kv
    raw = "volume: 80\nstate: play\nelapsed: 12.500\nduration: 213.000\nOK\n"
    d = _parse_kv(raw)
    assert d == {"volume": "80", "state": "play",
                 "elapsed": "12.500", "duration": "213.000"}


class _FakeMusic:
    def __init__(self, status, song):
        self._status, self._song = status, song

    def status_dict(self):
        return self._status

    def current_song(self):
        return self._song


def test_music_now_label_prefers_artist_title():
    m = _FakeMusic({}, {"Title": "Strobe", "Artist": "deadmau5",
                        "file": "yt:abc"})
    assert cli._music_now_label(m) == "deadmau5 — Strobe"


def test_music_now_label_falls_back_to_name_then_file():
    assert cli._music_now_label(
        _FakeMusic({}, {"Name": "BBC R6"})) == "BBC R6"
    assert cli._music_now_label(
        _FakeMusic({}, {"file": "/music/x/song.mp3"})) == "song.mp3"


def test_music_status_line_idle_and_playing():
    idle = cli._music_status_line(_FakeMusic({"state": "stop"}, {}),
                                  width=8, hide_idle=False)
    assert idle == "○"
    playing = cli._music_status_line(
        _FakeMusic({"state": "play", "elapsed": "30", "duration": "120"}, {}),
        width=8, hide_idle=False)
    assert playing.startswith("▶ 00:30 ") and playing.endswith(" 02:00")


def test_strip_markdown_inline():
    from agent_media_core.intake.submit import _strip_markdown_inline as f
    assert f("use `media toggle` now") == "use media toggle now"
    assert f("this is **bold** and ~~gone~~") == "this is bold and gone"
    assert f("see [the docs](http://x/y) here") == "see the docs here"
    assert f("## Heading text") == "Heading text"
    # single * / _ left alone (often literal: identifiers, a*b)
    assert f("source_pane stays and a*b too") == "source_pane stays and a*b too"


def test_history_index_for_pane(monkeypatch):
    rows = [
        {"extras": {"source_pane": "%9"}},                    # 1 latest
        {"extras": '{"source_pane": "%4"}'},                  # 2 (json string)
        {"extras": {"source_pane": "%9"}},                    # 3
    ]
    monkeypatch.setattr(cli, "_speech_history", lambda n=20: rows)
    assert cli._history_index_for_pane("%9") == 1   # most recent for %9
    assert cli._history_index_for_pane("%4") == 2   # parses json extras
    assert cli._history_index_for_pane("%99") is None
    assert cli._history_index_for_pane("") is None


def test_toggle_idle_replays_active_pane(monkeypatch):
    replayed = {}

    def fake_replay(i):
        replayed["idx"] = i
        return 0

    monkeypatch.setattr(cli, "_get", lambda prop: True if prop == "idle-active" else None)
    monkeypatch.setattr(cli, "_do_replay", fake_replay)
    monkeypatch.setattr(cli, "_speech_history",
                        lambda n=20: [{"extras": {"source_pane": "%1"}},
                                      {"extras": {"source_pane": "%7"}}])
    monkeypatch.setenv("TTS_POPUP_PANE", "%7")
    assert cli.cmd_toggle(object()) == 0
    assert replayed["idx"] == 2   # %7's most recent clip, not the global latest


def test_now_pane_falls_back_to_last_clip(monkeypatch, capsys):
    """When nothing is playing, now-pane uses the most recent clip's pane."""
    class FakeStore:
        def get_now_playing(self, sink):
            return None  # idle

        def recent_history(self, *, sink, limit):
            return [{"extras": {"source_pane": "%7"}}]

    captured = {}

    class _R:
        returncode = 0
        stdout = "my coding pane\n"

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _R()

    monkeypatch.setattr(cli, "StateStore", FakeStore)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.cmd_now_pane(object()) == 0
    # Resolved the *history* pane id, not the active pane.
    assert "%7" in captured["cmd"] and "#{pane_title}" in captured["cmd"]
    assert capsys.readouterr().out.strip() == "my coding pane"


class _FakeIpc:
    """Records mpv-IPC calls and answers get_property from a dict."""
    MpvIpcError = RuntimeError

    def __init__(self, props=None):
        self.props = props or {}
        self.calls = []

    def command(self, sock, *args):
        self.calls.append(("command", *args))
        return None

    def set_property(self, sock, name, value):
        self.calls.append(("set", name, value))
        self.props[name] = value

    def get_property(self, sock, name, timeout=2.0):
        self.calls.append(("get", name))
        return self.props.get(name)


def test_jump_end_unpauses_and_finishes_single_clip(monkeypatch):
    fake = _FakeIpc({"playlist-count": 1})
    monkeypatch.setattr(cli, "ipc", fake)
    monkeypatch.setattr(cli, "_sock", lambda: "/tmp/x.sock")

    class A:
        where = "end"
    assert cli.cmd_jump(A()) == 0
    # Clears pause + mute so the clip can actually play out to EOF...
    assert ("set", "pause", False) in fake.calls
    assert ("set", "mute", False) in fake.calls
    # ...does NOT touch playlist-pos for a single clip...
    assert not any(c[:2] == ("set", "playlist-pos") for c in fake.calls)
    # ...and seeks to the end last.
    assert fake.calls[-1] == ("command", "seek", 100, "absolute-percent")


def test_jump_end_lands_on_last_clip_of_playlist(monkeypatch):
    fake = _FakeIpc({"playlist-count": 3})
    monkeypatch.setattr(cli, "ipc", fake)
    monkeypatch.setattr(cli, "_sock", lambda: "/tmp/x.sock")

    class A:
        where = "end"
    assert cli.cmd_jump(A()) == 0
    # Jumps to the final playlist entry before seeking to its end.
    assert ("set", "playlist-pos", 2) in fake.calls
    assert fake.calls[-1] == ("command", "seek", 100, "absolute-percent")


def test_jump_start_seeks_zero(monkeypatch):
    fake = _FakeIpc()
    monkeypatch.setattr(cli, "ipc", fake)
    monkeypatch.setattr(cli, "_sock", lambda: "/tmp/x.sock")

    class A:
        where = "start"
    assert cli.cmd_jump(A()) == 0
    assert fake.calls == [("command", "seek", 0, "absolute")]


def test_ncmpcpp_pane_matches_command(monkeypatch):
    class _R:
        returncode = 0
        stdout = "%0\tzsh\n%3\tncmpcpp\n%5\tclaude\n"

    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _R())
    assert cli._ncmpcpp_pane() == "%3"


def test_goto_track_focuses_and_jumps(monkeypatch):
    calls = []

    class _R:
        returncode = 0
        stdout = "%3\tncmpcpp\n"

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.cmd_goto_track(object()) == 0
    # Focused the ncmpcpp pane and sent `o` (JumpToPlayingSong) to it.
    assert ["tmux", "select-pane", "-t", "%3"] in calls
    assert ["tmux", "send-keys", "-t", "%3", "o"] in calls


def test_goto_track_no_pane_returns_1(monkeypatch):
    class _R:
        returncode = 0
        stdout = "%0\tzsh\n%5\tclaude\n"

    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _R())
    # No ncmpcpp pane → rc 1 so the popup shows a hint instead of closing.
    assert cli.cmd_goto_track(object()) == 1


def test_replay_resolves_history(monkeypatch):
    played = {}

    class FakeStore:
        def recent_history(self, *, sink, limit):
            return [{"uri": "/clips/latest.mp3", "text": "a"},
                    {"uri": "/clips/older.mp3", "text": "b"}][:limit]

        def set_now_playing(self, sink, *, uri, started_at, target, extras):
            # _do_replay always refreshes now_playing so the status-bar
            # progress reflects the clip just started, even single-clip ones.
            played["now_playing_uri"] = uri

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
    assert played["now_playing_uri"] == "/clips/older.mp3"
