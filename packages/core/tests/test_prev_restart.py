"""Popup `<` (speech) has music-player ⏮ semantics: restart the current turn
if we're past its start, only step back a turn when at/near the start or idle.

`cmd_replay_prev` decides from the current turn's elapsed position
(_speech_display_state's `pos`) vs MEDIA_POPUP_PREV_RESTART_S, performs the
replay, and echoes the resolved history cursor for the popup to adopt.
"""

import argparse

import pytest

from agent_media_core import cli


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    """Keep the double-press breadcrumb out of the real state dir — and out of
    the next test, which would otherwise read this one's restart as its own."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_POPUP_PREV_DOUBLE_S", raising=False)


@pytest.fixture
def spy(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_anchor_session", lambda: "aaa")

    def fake_replay(index, session=None):
        calls.append(index)
        # Mimic _do_replay: nonzero when the index is past the end of history.
        return 0 if index <= fake_replay.max_idx else 1
    fake_replay.max_idx = 5
    monkeypatch.setattr(cli, "_do_replay", fake_replay)
    # No playlist to seek: the *live* readout these tests describe, where each
    # sentence is its own loadfile. Stubbed rather than left alone because the
    # real one asks the speech player — the actual phone, from a unit test.
    # That went unnoticed while the phone refused `seek` and the call failed
    # into this same answer; the day the app learned to seek, two tests started
    # exercising a live playlist on a device instead of the branch they name.
    monkeypatch.setattr(cli, "_restart_current_playlist", lambda: 1)

    def set_state(idle, pos):
        # (idle, pos, dur, paused, muted, speed, playing)
        monkeypatch.setattr(cli, "_speech_display_state",
                            lambda: (idle, pos, None, False, False, 1.0, not idle))
    return calls, set_state, fake_replay


def _run(idx, env, monkeypatch, default=None):
    if default is None:
        monkeypatch.delenv("MEDIA_POPUP_PREV_RESTART_S", raising=False)
    else:
        monkeypatch.setenv("MEDIA_POPUP_PREV_RESTART_S", default)
    return cli.cmd_replay_prev(argparse.Namespace(idx=idx))


def test_past_start_restarts_current_turn(spy, monkeypatch, capsys):
    calls, set_state, _ = spy
    set_state(idle=False, pos=30.0)          # 30s into the current turn
    assert _run(1, {}, monkeypatch) == 0
    assert calls == [1]                       # replayed the same clip (restart)
    assert capsys.readouterr().out.strip() == "1"   # cursor stays put


def test_near_start_steps_back_a_turn(spy, monkeypatch, capsys):
    calls, set_state, _ = spy
    set_state(idle=False, pos=1.0)            # only 1s in → within grace
    assert _run(1, {}, monkeypatch) == 0
    assert calls == [2]                       # stepped to the older turn
    assert capsys.readouterr().out.strip() == "2"


def test_idle_steps_back_even_if_pos_stale(spy, monkeypatch, capsys):
    calls, set_state, _ = spy
    set_state(idle=True, pos=99.0)            # nothing playing → step back
    assert _run(1, {}, monkeypatch) == 0
    assert calls == [2]
    assert capsys.readouterr().out.strip() == "2"


def test_step_back_stays_put_when_no_older_turn(spy, monkeypatch, capsys):
    calls, set_state, fake_replay = spy
    fake_replay.max_idx = 3
    set_state(idle=False, pos=0.0)
    assert _run(3, {}, monkeypatch) == 0
    assert calls == [4]                       # attempted older; it failed
    assert capsys.readouterr().out.strip() == "3"   # cursor held at 3


def test_threshold_is_env_tunable(spy, monkeypatch, capsys):
    calls, set_state, _ = spy
    set_state(idle=False, pos=6.0)
    # With a 10s grace, 6s in is still "near the start" → step back.
    assert _run(1, {}, monkeypatch, default="10") == 0
    assert calls == [2]
    _ = capsys.readouterr()
    # With a 2s grace, the same 6s → restart the current turn.
    set_state(idle=False, pos=6.0)
    assert _run(1, {}, monkeypatch, default="2") == 0
    assert calls == [2, 1]


# --- music / book `<` share the same restart-first grace window -------------

def _prev_with_restart_calls(monkeypatch, pos, grace=None):
    """Drive _prev_with_restart with a fake elapsed and record which arm fired."""
    if grace is None:
        monkeypatch.delenv("MEDIA_POPUP_PREV_RESTART_S", raising=False)
    else:
        monkeypatch.setenv("MEDIA_POPUP_PREV_RESTART_S", grace)
    fired = []
    cli._prev_with_restart(
        elapsed=lambda: pos,
        restart=lambda: fired.append("restart"),
        step_back=lambda: fired.append("step_back"),
    )
    return fired


def test_prev_with_restart_past_start_restarts(monkeypatch):
    assert _prev_with_restart_calls(monkeypatch, pos=30.0) == ["restart"]


def test_prev_with_restart_near_start_steps_back(monkeypatch):
    assert _prev_with_restart_calls(monkeypatch, pos=1.0) == ["step_back"]


def test_prev_with_restart_idle_steps_back(monkeypatch):
    # None elapsed (idle / no track) coerces to 0 → step back, never crashes.
    assert _prev_with_restart_calls(monkeypatch, pos=None) == ["step_back"]


def test_prev_with_restart_honors_grace(monkeypatch):
    assert _prev_with_restart_calls(monkeypatch, pos=6.0, grace="10") == ["step_back"]
    assert _prev_with_restart_calls(monkeypatch, pos=6.0, grace="2") == ["restart"]


# --- restarting a turn the history has never heard of -----------------------
#
# The remote-render lane (a reply rendered on the phone) records now_playing
# and writes NO speech-history row. `<` used to resolve its restart through
# history, scoped to the playing clip's conversation — which matched nothing,
# so it printed "no clip to replay" to a stderr the popup discards and did
# nothing at all. The key looked dead for the whole of that reply.

@pytest.fixture
def playing(monkeypatch):
    """Something is playing, 30s in, from conversation `bbb`."""
    monkeypatch.setattr(cli, "_speech_display_state",
                        lambda: (False, 30.0, None, False, False, 1.0, True))
    monkeypatch.setattr(cli, "_now_speaking",
                        lambda: {"extras": {"source_session": "bbb"}})
    monkeypatch.setattr(cli, "_anchor_session", lambda: None)
    monkeypatch.delenv("MEDIA_POPUP_PREV_RESTART_S", raising=False)


def _ipc_spy(monkeypatch, playlist_count=1):
    """Record ipc traffic; `playlist-count` answers with the given depth."""
    seen = []
    monkeypatch.setattr(cli, "_sock", lambda: "/nope.sock")
    monkeypatch.setattr(cli.ipc, "get_property",
                        lambda s, p, **k: playlist_count if p == "playlist-count" else None)
    monkeypatch.setattr(cli.ipc, "set_property",
                        lambda s, p, v, **k: seen.append(("set", p, v)))
    monkeypatch.setattr(cli.ipc, "command",
                        lambda s, *a, **k: seen.append(("cmd", *a)))
    return seen


def test_queued_turn_restarts_in_place(playing, monkeypatch, capsys):
    """A replayed turn is one mpv playlist: index 0 + seek 0 IS its start —
    no history lookup, no re-push over the bridge."""
    seen = _ipc_spy(monkeypatch, playlist_count=4)
    replays = []
    monkeypatch.setattr(cli, "_do_replay",
                        lambda i, session=None: replays.append(i) or 0)
    assert cli.cmd_replay_prev(argparse.Namespace(idx=1)) == 0
    assert replays == []                                   # never touched history
    assert ("set", "playlist-pos", 0) in seen
    assert ("cmd", "seek", 0, "absolute") in seen
    assert capsys.readouterr().out.strip() == "1"          # cursor stays put


def test_unrecorded_turn_seeks_to_zero(playing, monkeypatch, capsys):
    """No history row for the playing conversation → seek what IS audible back
    to zero. The one thing it must not do is nothing."""
    seen = _ipc_spy(monkeypatch, playlist_count=1)          # single loadfile
    monkeypatch.setattr(cli, "_speech_history",
                        lambda n=20, session=None, include_live=False: [])      # nothing recorded
    replays = []
    monkeypatch.setattr(cli, "_do_replay",
                        lambda i, session=None: replays.append(i) or 0)
    assert cli.cmd_replay_prev(argparse.Namespace(idx=1)) == 0
    assert replays == []            # must NOT re-push another conversation's clip
    assert ("cmd", "seek", 0, "absolute") in seen


def test_recorded_turn_still_replays_from_history(playing, monkeypatch, capsys):
    """A live readout is one loadfile per sentence, so restarting the *turn*
    still means re-pushing it — when there is a row to re-push."""
    _ipc_spy(monkeypatch, playlist_count=1)
    monkeypatch.setattr(cli, "_speech_history",
                        lambda n=20, session=None, include_live=False: [{"uri": "x"}])
    replays = []
    monkeypatch.setattr(cli, "_do_replay",
                        lambda i, session=None: replays.append((i, session)) or 0)
    assert cli.cmd_replay_prev(argparse.Namespace(idx=1)) == 0
    assert replays == [(1, "bbb")]          # scoped to the conversation playing


def test_a_queued_turn_is_restarted_in_place(spy, monkeypatch, capsys):
    """The other branch: a replayed turn is one playlist, so `<` seeks it back
    to its own start — no history row, and no re-push over the bridge."""
    calls, set_state, _ = spy
    set_state(idle=False, pos=30.0)
    monkeypatch.setattr(cli, "_restart_current_playlist", lambda: 0)
    assert _run(3, {}, monkeypatch) == 0
    assert calls == [], "seeking the playlist back was enough; nothing to replay"
    assert capsys.readouterr().out.strip() == "3", "the cursor must stay put"


# --- the double press -------------------------------------------------------
#
# The reported bug: `<` twice restarted the same clip both times. Two causes,
# one symptom. This is the second — on the phone lane a press costs a second or
# two of round trips, so the position the second press reads is already past
# the grace window even though the listener pressed twice in a breath.

def test_double_press_steps_back_despite_position(spy, monkeypatch, capsys):
    calls, set_state, _ = spy
    set_state(idle=False, pos=30.0)
    assert _run(1, {}, monkeypatch) == 0
    assert calls == [1]                        # first press: restart
    _ = capsys.readouterr()
    set_state(idle=False, pos=3.4)             # the restart, plus press latency
    assert _run(1, {}, monkeypatch) == 0
    assert calls == [1, 2]                     # second press: the older turn
    assert capsys.readouterr().out.strip() == "2"


def test_latch_is_consumed_by_one_press(spy, monkeypatch, capsys):
    """Held down, `<` walks back a turn per press — it must not step once and
    then restart whatever it landed on."""
    calls, set_state, _ = spy
    set_state(idle=False, pos=30.0)
    _run(1, {}, monkeypatch)                   # restart, breadcrumb dropped
    set_state(idle=False, pos=3.4)
    _run(1, {}, monkeypatch)                   # steps to 2, breadcrumb eaten
    set_state(idle=False, pos=30.0)            # a later press, well past a start
    _run(2, {}, monkeypatch)
    assert calls == [1, 2, 2]                  # restarted 2, did not walk on


def test_latch_ignores_a_different_cursor(spy, monkeypatch, capsys):
    """The breadcrumb answers for the turn it restarted, not for wherever the
    walk has since moved."""
    calls, set_state, _ = spy
    set_state(idle=False, pos=30.0)
    _run(1, {}, monkeypatch)                   # restarted cursor 1
    set_state(idle=False, pos=30.0)
    _run(4, {}, monkeypatch)                   # a different cursor → restart
    assert calls == [1, 4]


def test_latch_expires(spy, monkeypatch, capsys):
    calls, set_state, _ = spy
    set_state(idle=False, pos=30.0)
    monkeypatch.setenv("MEDIA_POPUP_PREV_DOUBLE_S", "0")   # latch disabled
    _run(1, {}, monkeypatch)
    set_state(idle=False, pos=3.4)
    _run(1, {}, monkeypatch)
    assert calls == [1, 1]                     # position rule alone: restart
