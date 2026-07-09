"""Tests for the media CLI pure helpers + parser (task #9, PR A)."""

import pytest

from agent_media_core import cli


def test_fmt_mmss():
    assert cli.fmt_mmss(None) == "--:--"
    assert cli.fmt_mmss(0) == "00:00"
    assert cli.fmt_mmss(8) == "00:08"
    assert cli.fmt_mmss(75) == "01:15"
    assert cli.fmt_mmss(-3) == "00:00"


def test_fmt_time():
    assert cli.fmt_time(None) == "--:--"
    # auto: sub-hour stays MM:SS (same as fmt_mmss); >= 1h → compact H:MM
    assert cli.fmt_time(8) == "00:08"
    assert cli.fmt_time(75) == "01:15"
    assert cli.fmt_time(3600) == "1:00"
    assert cli.fmt_time(39937) == "11:05"          # 11h05m audiobook
    assert cli.fmt_time(-3) == "00:00"
    # forced format keeps a pos/total pair consistent
    assert cli.fmt_time(6932, hours=True) == "1:55"   # 1h55m pos
    assert cli.fmt_time(150, hours=True) == "0:02"    # 2.5-min pos in an hours total
    assert cli.fmt_time(150, hours=False) == "02:30"


def test_render_status_compact_book():
    # An 11h book renders compactly, not the overflowing 115:32 / 665:37.
    line = cli.render_status(idle=False, pos=6932, dur=39937,
                             paused=True, muted=False, bar=False)
    assert line == "⏸ 1:55 / 11:05"
    # A short clip stays MM:SS (unchanged from before).
    line = cli.render_status(idle=False, pos=8, dur=225,
                             paused=False, muted=False, bar=False)
    assert line == "▶ 00:08 / 03:45"


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


def test_toggle_idle_replays_active_pane(monkeypatch, tmp_path):
    # Isolate the state store: the remote-speech branch consults the real
    # now_playing mirror, so a test run during live playback flips branches.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
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


class _NowPaneStore:
    """Idle store stub for now-pane; `muted` controls the 🔒 prefix."""
    def __init__(self, muted=False):
        self._muted = muted

    def get_now_playing(self, sink):
        return None  # idle → subject is the caller pane

    def resolve_mute(self, pane, sess):
        return self._muted


def test_now_pane_uses_caller_pane_when_idle(monkeypatch, capsys):
    """Idle → now-pane names the popup's caller pane (the subject)."""
    captured = {}

    class _R:
        returncode = 0
        # window_name \t pane_title — pane title carries a spinner glyph.
        stdout = "my coding pane\t⠐ doing a thing\n"

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _R()

    monkeypatch.setattr(cli, "StateStore", _NowPaneStore)
    monkeypatch.setattr(cli, "_caller_pane", lambda: "%7")
    monkeypatch.setattr(cli, "_pane_alive", lambda p: True)
    monkeypatch.setattr(cli, "_tmux_session_for_pane", lambda p: "")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.cmd_now_pane(object()) == 0
    # Resolved the caller pane id, and asked for window name + pane title.
    assert "%7" in captured["cmd"]
    assert "#{window_name}\t#{pane_title}" in captured["cmd"]
    # Prefers the stable window name over the transient (spinner) pane title.
    # No ↪/🔒 prefix: subject is the caller, and it's unmuted.
    assert capsys.readouterr().out.strip() == "my coding pane"


def test_now_pane_strips_spinner_when_window_unnamed(monkeypatch, capsys):
    """A default-named window falls back to the spinner-stripped pane title."""
    class _R:
        returncode = 0
        stdout = "zsh\t⠐ Check the dotfiles repos\n"

    monkeypatch.setattr(cli, "StateStore", _NowPaneStore)
    monkeypatch.setattr(cli, "_caller_pane", lambda: "%7")
    monkeypatch.setattr(cli, "_pane_alive", lambda p: True)
    monkeypatch.setattr(cli, "_tmux_session_for_pane", lambda p: "")
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: _R())
    assert cli.cmd_now_pane(object()) == 0
    assert capsys.readouterr().out.strip() == "Check the dotfiles repos"


def test_now_pane_prefixes_following_and_muted(monkeypatch, capsys):
    """A clip playing in another pane → '↪ '; muted subject → '🔒 '."""
    class _Store:
        def get_now_playing(self, sink):
            return {"extras": {"source_pane": "%30",
                               "source_tmux_session": "ts"}}

        def resolve_mute(self, pane, sess):
            return True

    class _R:
        returncode = 0
        stdout = "youtube-accounts\t⠐ x\n"

    monkeypatch.setattr(cli, "StateStore", _Store)
    monkeypatch.setattr(cli, "_caller_pane", lambda: "%27")  # different → following
    monkeypatch.setattr(cli, "_pane_alive", lambda p: True)  # subject pane is live
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: _R())
    assert cli.cmd_now_pane(object()) == 0
    assert capsys.readouterr().out.strip() == "↪ 🔒 youtube-accounts"


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


def test_focus_pane_switches_client_to_target_session(monkeypatch):
    """Focus must `switch-client` to the pane's session, else cross-session
    'go to' silently no-ops (select-window/-pane only move the target session,
    not the attached client)."""
    calls = []

    class _R:
        returncode = 0
        stdout = "music\n"  # session_name lookup for the pane

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli._focus_pane("%10")
    assert ["tmux", "select-pane", "-t", "%10"] in calls
    assert ["tmux", "switch-client", "-t", "music"] in calls


def test_open_ncmpcpp_opens_window(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd))
    monkeypatch.delenv("MEDIA_NCMPCPP_CMD", raising=False)
    assert cli.cmd_open_ncmpcpp(object()) == 0
    assert ["tmux", "new-window", "ncmpcpp"] in calls


def test_open_ncmpcpp_honors_cmd_override(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd))
    monkeypatch.setenv("MEDIA_NCMPCPP_CMD", "/opt/bin/ncmpcpp -q")
    assert cli.cmd_open_ncmpcpp(object()) == 0
    assert ["tmux", "new-window", "/opt/bin/ncmpcpp -q"] in calls


# --- goto-pane: focus live pane, else offer to resume / report closed ---

def _patch_pane_alive(monkeypatch, alive_panes):
    """Make _pane_alive report only `alive_panes` as open."""
    class _R:
        returncode = 0
        stdout = "\n".join(alive_panes) + "\n"
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _R())


def test_goto_pane_focuses_live_pane(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_spoken_pane", lambda: "%7")
    monkeypatch.setattr(cli, "_spoken_session", lambda: "sess-xyz")
    monkeypatch.setattr(cli, "_pane_alive", lambda p: True)
    monkeypatch.setattr(cli, "_focus_pane", lambda p: calls.append(p))
    assert cli.cmd_goto_pane(object()) == 0  # live → just focus
    assert calls == ["%7"]


def test_goto_pane_closed_with_session_returns_3(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_spoken_pane", lambda: "%7")
    monkeypatch.setattr(cli, "_spoken_session", lambda: "sess-xyz")
    monkeypatch.setattr(cli, "_pane_alive", lambda p: False)  # pane closed
    monkeypatch.setattr(cli, "_focus_pane",
                        lambda p: pytest.fail("should not focus a dead pane"))
    assert cli.cmd_goto_pane(object()) == 3
    assert capsys.readouterr().out.strip() == "sess-xyz"  # id for the popup


def test_goto_pane_closed_no_session_returns_2(monkeypatch):
    monkeypatch.setattr(cli, "_spoken_pane", lambda: "%7")
    monkeypatch.setattr(cli, "_spoken_session", lambda: None)
    monkeypatch.setattr(cli, "_pane_alive", lambda p: False)
    assert cli.cmd_goto_pane(object()) == 2


def test_goto_pane_no_source_returns_1(monkeypatch):
    monkeypatch.setattr(cli, "_spoken_pane", lambda: None)
    monkeypatch.setattr(cli, "_spoken_session", lambda: None)
    assert cli.cmd_goto_pane(object()) == 1


def test_pane_alive_membership(monkeypatch):
    class _R:
        returncode = 0
        stdout = "%1\n%7\n%10\n"
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _R())
    assert cli._pane_alive("%7") is True
    assert cli._pane_alive("%99") is False
    assert cli._pane_alive("") is False


def test_open_session_resumes(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd))
    # Resume must run in the session's own project cwd, else `claude --resume`
    # can't find the id and the window closes instantly.
    monkeypatch.setattr(cli, "_session_cwd", lambda sid: "/home/ryer/proj")

    class A:
        session = "abc-123"
    assert cli.cmd_open_session(A()) == 0
    assert ["tmux", "new-window", "-c", "/home/ryer/proj",
            "env -u ANTHROPIC_API_KEY claude --resume abc-123"] in calls


def test_open_session_resumes_without_cwd(monkeypatch):
    """No transcript found → resume without -c (best effort), key still stripped."""
    calls = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(cli, "_session_cwd", lambda sid: None)

    class A:
        session = "abc-123"
    assert cli.cmd_open_session(A()) == 0
    assert ["tmux", "new-window",
            "env -u ANTHROPIC_API_KEY claude --resume abc-123"] in calls


def test_open_session_blank_returns_1(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: pytest.fail("should not spawn"))

    class A:
        session = "  "
    assert cli.cmd_open_session(A()) == 1


def test_spoken_session_reads_extras(monkeypatch):
    monkeypatch.setattr(cli, "_now_speaking",
                        lambda: {"extras": {"source_session": "live-sess"}})
    assert cli._spoken_session() == "live-sess"
    # Idle: falls back to the most recent history clip's extras.
    monkeypatch.setattr(cli, "_now_speaking", lambda: None)
    monkeypatch.setattr(cli, "_speech_history",
                        lambda n=1: [{"extras": {"source_session": "hist-sess"}}])
    assert cli._spoken_session() == "hist-sess"


def test_split_with_paragraphs_preserves_boundaries():
    from agent_media_core.intake.submit import (
        _split_sentences_with_paragraphs as seg)
    s, p = seg("First one. Second here is a bit longer.\n\n"
               "New paragraph opener here.\n\nThird block of text.")
    assert len(s) == len(p)
    assert p[0] == 0 and p[-1] == 2
    # A short sentence that opens a new paragraph is NOT merged backward.
    s2, p2 = seg("A longer first sentence here.\n\nYes.")
    assert s2[-1] == "Yes." and p2[-1] == 1


def test_nav_target_sentence_and_paragraph():
    para = [0, 0, 1, 2]
    n = 4
    assert cli._nav_target(0, n, para, "sentence", 1) == 1
    assert cli._nav_target(0, n, para, "sentence", -1) == -1   # caller clamps
    assert cli._nav_target(0, n, para, "paragraph", 1) == 2    # → para 1 start
    assert cli._nav_target(2, n, para, "paragraph", 1) == 3    # → para 2 start
    assert cli._nav_target(3, n, para, "paragraph", 1) == 4    # past end → end
    assert cli._nav_target(3, n, para, "paragraph", -1) == 2   # prev para start
    assert cli._nav_target(2, n, para, "paragraph", -1) == 0   # prev para start


def _skip_args(unit="sentence", direction=1, fallback=5.0):
    class A:
        pass
    A.unit, A.dir, A.seek_fallback = unit, direction, fallback
    return A()


def test_skip_falls_back_to_time_seek_without_sequence(monkeypatch):
    fake = _FakeIpc({"idle-active": False, "playlist-count": 1})
    monkeypatch.setattr(cli, "ipc", fake)
    monkeypatch.setattr(cli, "_sock", lambda: "/s")

    class FakeStore:
        def get_now_playing(self, sink):
            return {"extras": {"clip_sentences": ["only one sentence"]}}

    monkeypatch.setattr(cli, "StateStore", FakeStore)
    assert cli.cmd_skip(_skip_args(fallback=5.0)) == 0
    assert ("command", "seek", 5.0, "relative") in fake.calls


def test_skip_live_writes_nav_request(monkeypatch, tmp_path):
    # Breadcrumb chaining: presses inside the chain window step from the LAST
    # press's target, not a re-read of current_sentence_idx (which lags a
    # rapid second press on a remote target). Isolate the crumb in tmp.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    fake = _FakeIpc({"idle-active": False, "playlist-count": 1})
    monkeypatch.setattr(cli, "ipc", fake)
    monkeypatch.setattr(cli, "_sock", lambda: "/s")
    written = {}
    monkeypatch.setattr(cli, "_write_nav_request",
                        lambda i, *a: written.__setitem__("i", i))

    class FakeStore:
        def get_now_playing(self, sink):
            return {"extras": {"clip_sentences": ["a", "b", "c"],
                               "clip_paragraph_idx": [0, 0, 1],
                               "current_sentence_idx": 0}}

    monkeypatch.setattr(cli, "StateStore", FakeStore)
    # sentence forward → next index
    assert cli.cmd_skip(_skip_args("sentence", 1)) == 0
    assert written["i"] == 1
    # paragraph forward CHAINS from the last target (1, paragraph 0) → first
    # clip of paragraph 1 (idx 2), even though the mirror still says 0
    assert cli.cmd_skip(_skip_args("paragraph", 1)) == 0
    assert written["i"] == 2
    # sentence back chains from 2 → 1 (the mirror's stale 0 is ignored)
    assert cli.cmd_skip(_skip_args("sentence", -1)) == 0
    assert written["i"] == 1
    # expired crumb → back from the mirror's 0 clamps to 0 (restart first)
    cli._skip_cursor_path().unlink()
    assert cli.cmd_skip(_skip_args("sentence", -1)) == 0
    assert written["i"] == 0


def test_skip_playlist_sets_playlist_pos_and_highlights(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    fake = _FakeIpc({"idle-active": False, "playlist-count": 3,
                     "playlist-pos": 0})
    monkeypatch.setattr(cli, "ipc", fake)
    monkeypatch.setattr(cli, "_sock", lambda: "/s")
    hl = {}
    monkeypatch.setattr(cli, "_force_highlight_sentence",
                        lambda s: hl.__setitem__("s", s))

    class FakeStore:
        def get_now_playing(self, sink):
            return {"extras": {"clip_sentences": ["a", "b", "c"],
                               "clip_paragraph_idx": [0, 1, 2]}}

    monkeypatch.setattr(cli, "StateStore", FakeStore)
    assert cli.cmd_skip(_skip_args("sentence", 1)) == 0
    assert ("set", "playlist-pos", 1) in fake.calls
    assert hl["s"] == "b"


def test_replay_resolves_history(monkeypatch):
    played = {}

    class FakeStore:
        def get_now_playing(self, sink):
            return None

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


# --- replay-at-cursor (cursor -> clip) -------------------------------------

# Single-line texts so _anchor_for returns the whole line; ordered newest-first
# the way _speech_history yields them.
_RAC_ROWS = [
    {"text": "Alpha row one is the most recent reply we made."},   # idx 1
    {"text": "Bravo row two is also a fairly recent reply here."},  # idx 2
    {"text": "Charlie row three sits just above the cursor now."},  # idx 3
    {"text": "Delta row four is older content further down below."},  # idx 4
]


def _rac_fake_run(cursor_line, captured, *, rec=None):
    """Build a subprocess.run stub for cmd_replay_at_cursor.

    cursor_line: the "pane_in_mode\\tcopy_cursor_y\\tscroll_position" string the
    cursor display-message query returns. captured: capture-pane output. rec
    (optional dict) records the capture-pane "-E" value the function computed.
    """
    def run(cmd, **kw):
        class _R:
            returncode = 0
            stdout = ""
        r = _R()
        if "capture-pane" in cmd:
            if rec is not None and "-E" in cmd:
                rec["end"] = cmd[cmd.index("-E") + 1]
            r.stdout = captured
        elif any("#{pane_in_mode}" in str(x) for x in cmd):
            r.stdout = cursor_line + "\n"
        return r
    return run


def test_replay_at_cursor_picks_nearest_preceding(monkeypatch):
    """In copy-mode: replay the most recent clip whose text is above the cursor,
    and convert copy_cursor_y/scroll_position to the capture-pane end line."""
    rec = {}
    played = {}
    captured = _RAC_ROWS[2]["text"] + "\n" + _RAC_ROWS[3]["text"]  # rows 3,4 only

    monkeypatch.setenv("TTS_POPUP_PANE", "%5")
    monkeypatch.setattr(cli, "_tmux_session_for_pane", lambda p: "sess")
    monkeypatch.setattr(cli, "_speech_history",
                        lambda n=20, session=None: _RAC_ROWS)
    monkeypatch.setattr(cli, "_do_replay",
                        lambda i, session=None: played.update(idx=i, sess=session) or 0)
    # copy_cursor_y=10, scroll_position=4  ->  end line 10-4 = 6
    monkeypatch.setattr(cli.subprocess, "run",
                        _rac_fake_run("1\t10\t4", captured, rec=rec))

    assert cli.cmd_replay_at_cursor(object()) == 0
    assert played["idx"] == 3            # nearest clip above the cursor
    assert played["sess"] is None        # NOT session-scoped: the pane capture
                                         # is the scope, so any session's clip
                                         # visible above the cursor can play
    assert rec["end"] == "6"             # cursor_y - scroll_position


def test_replay_at_cursor_not_in_copy_mode_falls_back(monkeypatch):
    """No copy-mode cursor -> replay this pane's latest clip."""
    played = {}
    monkeypatch.setenv("TTS_POPUP_PANE", "%5")
    monkeypatch.setattr(cli, "_tmux_session_for_pane", lambda p: "sess")
    monkeypatch.setattr(cli, "_history_index_for_pane", lambda p: 2)
    monkeypatch.setattr(cli, "_do_replay",
                        lambda i, session=None: played.update(idx=i) or 0)
    monkeypatch.setattr(cli.subprocess, "run",
                        _rac_fake_run("0\t\t", ""))  # pane_in_mode=0

    assert cli.cmd_replay_at_cursor(object()) == 0
    assert played["idx"] == 2


def test_replay_at_cursor_fullscreen_matches_visible(monkeypatch):
    """Not in copy-mode (e.g. Claude fullscreen): replay the most recent clip
    on the visible screen — no cursor needed — rather than blindly the pane's
    latest clip."""
    played = {}
    # Visible screen shows Charlie (idx 3); the pane's "latest" would be idx 1.
    visible = "some header line\n" + _RAC_ROWS[2]["text"] + "\n> a prompt"
    monkeypatch.setenv("TTS_POPUP_PANE", "%5")
    monkeypatch.setattr(cli, "_tmux_session_for_pane", lambda p: "sess")
    monkeypatch.setattr(cli, "_speech_history",
                        lambda n=20, session=None: _RAC_ROWS)
    monkeypatch.setattr(cli, "_history_index_for_pane", lambda p: 1)
    monkeypatch.setattr(cli, "_do_replay",
                        lambda i, session=None: played.update(idx=i) or 0)
    monkeypatch.setattr(cli.subprocess, "run",
                        _rac_fake_run("0\t\t", visible))  # pane_in_mode=0

    assert cli.cmd_replay_at_cursor(object()) == 0
    assert played["idx"] == 3   # the on-screen clip, not _history_index_for_pane's 1


def test_replay_at_cursor_matches_wrapped_anchor(monkeypatch):
    """A clip's anchor word-wrapped across visual rows still matches.

    The terminal wraps a long line at its content width, so the anchor spans
    two captured rows. cmd_replay_at_cursor collapses whitespace before the
    substring test, so the wrap must not defeat the match.
    """
    played = {}
    # Row 3's anchor is one logical line, but the captured pane wrapped it:
    a = "Charlie row three sits just above the cursor now."
    wrapped = "Charlie row three sits just above the\ncursor now."  # mid-line break
    assert a not in wrapped                                          # raw test would miss
    captured = wrapped + "\n" + _RAC_ROWS[3]["text"]
    monkeypatch.setenv("TTS_POPUP_PANE", "%5")
    monkeypatch.setattr(cli, "_tmux_session_for_pane", lambda p: "sess")
    monkeypatch.setattr(cli, "_speech_history",
                        lambda n=20, session=None: _RAC_ROWS)
    monkeypatch.setattr(cli, "_do_replay",
                        lambda i, session=None: played.update(idx=i) or 0)
    monkeypatch.setattr(cli.subprocess, "run",
                        _rac_fake_run("1\t10\t0", captured))

    assert cli.cmd_replay_at_cursor(object()) == 0
    assert played["idx"] == 3            # matched despite the wrap


def test_replay_at_cursor_no_match_above_cursor(monkeypatch):
    """In copy-mode but no clip's text is above the cursor -> error, no replay."""
    called = {"replay": False}
    monkeypatch.setenv("TTS_POPUP_PANE", "%5")
    monkeypatch.setattr(cli, "_tmux_session_for_pane", lambda p: "sess")
    monkeypatch.setattr(cli, "_speech_history",
                        lambda n=20, session=None: _RAC_ROWS)
    monkeypatch.setattr(cli, "_do_replay",
                        lambda i, session=None: called.update(replay=True) or 0)
    monkeypatch.setattr(cli.subprocess, "run",
                        _rac_fake_run("1\t3\t0", "unrelated text with no clip anchor"))

    assert cli.cmd_replay_at_cursor(object()) == 1
    assert called["replay"] is False


# --- book / focus / channels CLI ------------------------------------------

class _FakeSrv:
    """Records calls to the mcp_server tool functions the book CLI delegates
    to, returning canned dicts so the CLI never touches mpv/the store."""

    def __init__(self):
        self.calls = []

    def book_play(self, uri, resume=True, start_ms=-1, target=""):
        self.calls.append(("book_play", uri, resume, start_ms, target))
        return {"ok": True, "uri": uri, "resumed_from_ms": 0}

    def book_resume(self, target=""):
        self.calls.append(("book_resume", target)); return {"ok": True}

    def book_pause(self, target="local"):
        self.calls.append(("book_pause", target)); return {"ok": True}

    def book_stop(self, target="local"):
        self.calls.append(("book_stop", target)); return {"ok": True}

    def book_next(self, target=""):
        self.calls.append(("book_next", target)); return {"ok": True}

    def book_prev(self, target=""):
        self.calls.append(("book_prev", target))
        return {"ok": False, "reason": "at start of playlist"}

    def book_skip(self, seconds=30, target="local"):
        self.calls.append(("book_skip", seconds, target)); return {"ok": True}

    def book_speed(self, rate, target="local"):
        self.calls.append(("book_speed", rate, target))
        return {"ok": True, "speed": rate}

    def book_bed(self, mode, target="local"):
        self.calls.append(("book_bed", mode, target)); return {"ok": True}

    def book_now_playing(self, target="local"):
        return {"idle": True}

    def focus(self, channel, target="local"):
        self.calls.append(("focus", channel, target)); return {"ok": True}

    def channels_status(self):
        return {"focus": "book", "bed": "duck",
                "music": {"uri": None}, "book": {"idle": True}}

    def book_playlist_new(self, name):
        self.calls.append(("pl_new", name))
        return {"ok": True, "created": True}

    def book_playlist_add(self, name, uris):
        self.calls.append(("pl_add", name, uris))
        return {"ok": True, "added": len(uris), "count": len(uris)}

    def book_playlist_play(self, name, resume=True, target=""):
        self.calls.append(("pl_play", name, resume, target))
        return {"ok": True, "index": 0, "uri": "u", "title": None}

    def book_playlist_ls(self, name=""):
        return {"playlists": []} if not name else {
            "ok": True, "name": name, "cur_index": 0, "items": []}

    def book_playlist_rm(self, name):
        self.calls.append(("pl_rm", name)); return {"ok": True}


def _run(monkeypatch, fake, argv):
    monkeypatch.setattr(cli, "_srv", lambda: fake)
    return cli.main(argv)


def test_book_subcommands_registered():
    p = cli._build_parser()
    top = next(a for a in p._actions if a.choices and "book" in a.choices)
    for name in ("book", "focus", "channels"):
        assert name in top.choices
    book = top.choices["book"]
    bsub = next(a for a in book._actions if a.choices and "play" in a.choices)
    for name in ("play", "resume", "pause", "stop", "next", "prev", "skip",
                 "speed", "bed", "status", "now", "playlist"):
        assert name in bsub.choices, name


def test_book_play_passes_flags(monkeypatch):
    fake = _FakeSrv()
    assert _run(monkeypatch, fake,
                ["book", "play", "yt:foo", "--no-resume", "--target", "rooms"]) == 0
    assert ("book_play", "yt:foo", False, -1, "rooms") in fake.calls


def test_book_skip_default_is_plus_30(monkeypatch):
    fake = _FakeSrv()
    assert _run(monkeypatch, fake, ["book", "skip"]) == 0
    assert ("book_skip", 30.0, "local") in fake.calls


def test_book_failure_maps_to_exit_1(monkeypatch):
    fake = _FakeSrv()
    # book_prev returns ok=False in the fake.
    assert _run(monkeypatch, fake, ["book", "prev"]) == 1


def test_book_playlist_add_collects_uris(monkeypatch):
    fake = _FakeSrv()
    assert _run(monkeypatch, fake,
                ["book", "playlist", "add", "dune", "a", "b", "c"]) == 0
    assert ("pl_add", "dune", ["a", "b", "c"]) in fake.calls


def test_book_playlist_play_resume_flag(monkeypatch):
    fake = _FakeSrv()
    assert _run(monkeypatch, fake,
                ["book", "playlist", "play", "dune", "--no-resume"]) == 0
    assert ("pl_play", "dune", False, "") in fake.calls


def test_focus_and_channels(monkeypatch):
    fake = _FakeSrv()
    assert _run(monkeypatch, fake, ["focus", "music"]) == 0
    assert ("focus", "music", "local") in fake.calls
    assert _run(monkeypatch, fake, ["channels"]) == 0


# --- popup-channel resolution ---------------------------------------------

def _resolve(monkeypatch, capsys, playing, last):
    """Run `media popup-channel` with the play-probe and last-viewed mocked."""
    monkeypatch.setattr(cli, "_channel_is_playing", lambda c: c in playing)
    monkeypatch.setattr(cli, "_last_popup_channel", lambda: last)
    assert cli.main(["popup-channel"]) == 0
    return capsys.readouterr().out.strip()


def test_popup_channel_single_playing_wins(monkeypatch, capsys):
    # A lone playing channel is opened regardless of what was last viewed.
    assert _resolve(monkeypatch, capsys, {"book"}, "speech") == "book"
    assert _resolve(monkeypatch, capsys, {"music"}, "book") == "music"


def test_popup_channel_multiple_playing_falls_back_to_last(monkeypatch, capsys):
    # Music bed under speech → ambiguous → defer to the last-viewed channel.
    assert _resolve(monkeypatch, capsys, {"speech", "music"}, "music") == "music"


def test_popup_channel_none_playing_uses_last(monkeypatch, capsys):
    assert _resolve(monkeypatch, capsys, set(), "book") == "book"


def test_popup_channel_none_playing_no_memory_defaults_speech(monkeypatch, capsys):
    assert _resolve(monkeypatch, capsys, set(), None) == "speech"


def test_popup_channel_set_round_trips_through_file(monkeypatch, tmp_path):
    f = tmp_path / "popup-channel"
    monkeypatch.setattr(cli, "_popup_channel_file", lambda: f)
    assert cli.main(["popup-channel", "--set", "book"]) == 0
    assert f.read_text() == "book"
    assert cli._last_popup_channel() == "book"


def test_last_popup_channel_rejects_garbage(monkeypatch, tmp_path):
    f = tmp_path / "popup-channel"
    f.write_text("bogus")
    monkeypatch.setattr(cli, "_popup_channel_file", lambda: f)
    assert cli._last_popup_channel() is None
    # A missing file is fine too — no memory yet.
    monkeypatch.setattr(cli, "_popup_channel_file", lambda: tmp_path / "nope")
    assert cli._last_popup_channel() is None


def test_channel_is_playing_speech(monkeypatch):
    # idle-active False and pause False → playing; either truthy → not.
    monkeypatch.setattr(cli, "_get",
                        lambda p: {"idle-active": False, "pause": False}[p])
    assert cli._channel_is_playing("speech") is True
    monkeypatch.setattr(cli, "_get",
                        lambda p: {"idle-active": True, "pause": False}[p])
    assert cli._channel_is_playing("speech") is False


def test_channel_is_playing_music_reads_mpd_state(monkeypatch):
    state = {"v": "play"}
    monkeypatch.setattr(cli.SinkMusic, "status_dict",
                        lambda self, t: {"state": state["v"]})
    assert cli._channel_is_playing("music") is True
    state["v"] = "pause"
    assert cli._channel_is_playing("music") is False


def test_channel_is_playing_swallows_probe_errors(monkeypatch):
    def boom(self, t):
        raise OSError("MPD down")
    monkeypatch.setattr(cli.SinkMusic, "status_dict", boom)
    assert cli._channel_is_playing("music") is False
