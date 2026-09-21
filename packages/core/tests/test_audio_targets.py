"""The listener's choice of where speech (and the next music play) goes.

Nothing here plays audio or opens a socket: the speech path is stopped at
its first decision (`_ringer_hold`, which is handed the resolved target), and
the coordinator's broker writes are replaced by a recorder. The choice files
live in a per-test dir (conftest's `_no_listener_audio_choice`).
"""

from __future__ import annotations

import json
import os

import pytest

from agent_media_core import audio_targets, cli
from agent_media_core.types import Event, Source, Target


@pytest.fixture(autouse=True)
def plain_host(monkeypatch, tmp_path):
    """A host configured only by what each test sets: the real env file has
    already been loaded into os.environ by cli's import, and its per-target
    keys would decide which targets exist."""
    for k in list(os.environ):
        if k.startswith(("MEDIA_SPEECH_SOCKET_", "MEDIA_SPEECH_DEVICE_",
                         "MEDIA_REMOTE_SAY_CMD", "MEDIA_SPEECH_DEFAULT_TARGET",
                         "MEDIA_PHONE_PLAYER_URL", "MEDIA_MUSIC_LOCAL_ENDPOINT")):
            monkeypatch.delenv(k, raising=False)
    # Local brokers resolve to a socket under the state dir; point it at an
    # empty one so "is the player running" never looks at the real socket.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _phone_bridge(monkeypatch):
    """The phone as red5 sees it: a tcp bridge and a device that means
    'the broker's own default'."""
    monkeypatch.setenv("MEDIA_SPEECH_SOCKET_PHONE", "tcp://127.0.0.1:1")
    monkeypatch.setenv("MEDIA_SPEECH_DEVICE_PHONE", "")


# --- the rule -------------------------------------------------------------------

def test_no_choice_is_the_env_default(monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_DEFAULT_TARGET", "rooms")
    assert audio_targets.speech_override() is None
    assert audio_targets.speech_default() == "rooms"


def test_unset_env_is_local(monkeypatch):
    assert audio_targets.speech_default() == "local"


def test_the_choice_wins_over_the_env(monkeypatch):
    _phone_bridge(monkeypatch)
    monkeypatch.setenv("MEDIA_SPEECH_DEFAULT_TARGET", "phone")
    assert audio_targets.set_speech_override("rooms") == "rooms"
    assert audio_targets.speech_default() == "rooms"


def test_clearing_restores_the_env_default(monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_DEFAULT_TARGET", "rooms")
    audio_targets.set_speech_override("local")
    assert audio_targets.speech_default() == "local"
    assert audio_targets.set_speech_override(None) is None
    assert audio_targets.speech_default() == "rooms"
    audio_targets.set_speech_override(None)          # clearing twice is fine


@pytest.mark.parametrize("name", ["banana", "../etc", "ROOMS OFF", "x" * 40])
def test_unknown_names_are_refused(name):
    with pytest.raises(ValueError):
        audio_targets.set_speech_override(name)
    assert audio_targets.speech_override() is None


def test_a_known_target_this_host_cannot_play_is_refused():
    # `phone` with no socket, device or lane: the sink would raise.
    with pytest.raises(ValueError, match="not configured"):
        audio_targets.set_speech_override("phone")


def test_a_per_target_lane_makes_a_target_playable(monkeypatch):
    monkeypatch.setenv("MEDIA_REMOTE_SAY_CMD_APP", "ssh p8a media say")
    assert audio_targets.set_speech_override("app") == "app"


def test_a_target_named_only_by_env_is_offered(monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_DEVICE_KITCHEN", "pulse/kitchen")
    assert "kitchen" in [o["name"] for o in audio_targets.speech_options()]
    assert audio_targets.set_speech_override("kitchen") == "kitchen"


def test_a_stale_choice_falls_back_to_the_env(monkeypatch):
    """Config moves on under a saved choice: the choice is ignored, never
    followed into a target that would raise."""
    _phone_bridge(monkeypatch)
    monkeypatch.setenv("MEDIA_SPEECH_DEFAULT_TARGET", "rooms")
    audio_targets.set_speech_override("phone")
    monkeypatch.delenv("MEDIA_SPEECH_DEVICE_PHONE")
    monkeypatch.delenv("MEDIA_SPEECH_SOCKET_PHONE")
    assert audio_targets.speech_default() == "rooms"


def test_a_garbled_file_is_ignored(monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_DEFAULT_TARGET", "rooms")
    p = audio_targets._path(audio_targets.SPEECH_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\xff\xfe\x00junk")
    assert audio_targets.speech_default() == "rooms"


# --- the options ----------------------------------------------------------------

def test_options_list_only_what_this_host_can_use(monkeypatch):
    _phone_bridge(monkeypatch)
    opts = {o["name"]: o for o in audio_targets.speech_options()}
    assert set(opts) == {"phone", "rooms", "local"}
    assert opts["phone"]["label"] == "Phone (Termux player)"
    assert opts["rooms"]["label"] == "House speakers"
    assert all(set(o) == {"name", "label", "available", "why"} for o in opts.values())
    # A bridge is never probed: available, and nothing to say about it.
    assert opts["phone"]["available"] is True and opts["phone"]["why"] is None
    # No local broker socket in this state dir: say so.
    assert opts["local"]["available"] is False
    assert "no speech player" in opts["local"]["why"]


def test_a_running_broker_makes_local_available(monkeypatch, tmp_path):
    sock = tmp_path / "state" / "agent-media" / "sink-speech.sock"
    sock.parent.mkdir(parents=True)
    sock.touch()
    opts = {o["name"]: o for o in audio_targets.speech_options()}
    assert opts["local"]["available"] and opts["rooms"]["available"]


def test_a_breakered_bridge_says_so(monkeypatch):
    _phone_bridge(monkeypatch)
    from agent_media_core import _breaker
    monkeypatch.setattr(_breaker, "load", lambda ns: {"tcp://127.0.0.1:1": 9e12})
    opts = {o["name"]: o for o in audio_targets.speech_options()}
    assert opts["phone"]["available"] is True
    assert "slow or unreachable" in opts["phone"]["why"]


def test_the_block_names_default_and_override(monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_DEFAULT_TARGET", "rooms")
    b = audio_targets.speech_block()
    assert (b["current"], b["default"], b["overridden"]) == ("rooms", "rooms", False)
    audio_targets.set_speech_override("local")
    b = audio_targets.speech_block()
    assert (b["current"], b["default"], b["overridden"]) == ("local", "rooms", True)


# --- where a new reply goes -----------------------------------------------------

def test_a_new_reply_resolves_the_choice(monkeypatch):
    """submit resolves the target once, first thing — stop it there."""
    from agent_media_core.intake import submit

    seen = []
    monkeypatch.setattr(submit, "_ringer_hold",
                        lambda target, event: seen.append(target.name) or {"why": "test"})
    monkeypatch.setattr(submit, "_record_silenced", lambda *a, **k: 7)
    monkeypatch.setenv("MEDIA_SPEECH_DEFAULT_TARGET", "rooms")
    ev = Event(text="hello", source=Source.CLI)

    class _Nop:
        def __getattr__(self, name):
            return lambda *a, **k: None

    kw = dict(state=_Nop(), coordinator=_Nop(), sink=_Nop())
    assert submit._submit_event(ev, **kw) == 7
    audio_targets.set_speech_override("local")
    submit._submit_event(ev, **kw)
    audio_targets.set_speech_override(None)
    submit._submit_event(ev, **kw)
    # An event that names its own target keeps it.
    submit._submit_event(Event(text="x", source=Source.CLI, target=Target("rooms")), **kw)
    assert seen == ["rooms", "local", "rooms", "rooms"]


def test_the_coordinator_flags_follow_the_choice(monkeypatch):
    from agent_media_core.route import coordinator as co
    from agent_media_core.sinks import speech as sp

    got = []
    monkeypatch.setattr(sp, "set_speaking", lambda on, target: got.append(target.name))

    class _Nop:
        def __getattr__(self, name):
            return lambda *a, **k: None

    c = co.Coordinator(music=_Nop(), state=_Nop(), book=_Nop())
    audio_targets.set_speech_override("local")
    c._speaking(True)
    c._flag_writer.shutdown(wait=True)
    assert got == ["local"]


def test_the_cli_idle_target_and_replay_follow_the_choice(monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_DEFAULT_TARGET", "rooms")
    assert cli._speech_target() == Target("rooms")
    audio_targets.set_speech_override("local")
    assert cli._speech_target() == Target("local")


# --- `media speech-target` ------------------------------------------------------

def _cmd(argv):
    return cli.cmd_speech_target(cli._build_parser().parse_args(["speech-target", *argv]))


def test_cli_shows_sets_and_clears(monkeypatch, capsys):
    monkeypatch.setenv("MEDIA_SPEECH_DEFAULT_TARGET", "rooms")
    assert _cmd([]) == 0
    assert "speech → rooms (env default" in capsys.readouterr().out
    assert _cmd(["local"]) == 0
    out = capsys.readouterr().out
    assert "speech → local (chosen" in out and "next reply" in out
    assert audio_targets.speech_default() == "local"
    assert _cmd(["--clear"]) == 0
    capsys.readouterr()
    assert audio_targets.speech_default() == "rooms"


def test_cli_refuses_an_unknown_name(capsys):
    assert _cmd(["banana"]) == 2
    err = capsys.readouterr().err
    assert "refused" in err and "rooms" in err       # it says what there is
    assert audio_targets.speech_override() is None


def test_cli_json(monkeypatch, capsys):
    assert _cmd(["--json"]) == 0
    block = json.loads(capsys.readouterr().out)
    assert set(block) == {"current", "default", "overridden", "options"}


def test_cli_name_and_clear_together_is_an_error():
    assert _cmd(["local", "--clear"]) == 2


# --- music ----------------------------------------------------------------------

def test_music_pref_stands_in_for_default_only(monkeypatch):
    from agent_media_core.sinks import music_local

    monkeypatch.setattr(music_local, "configured", lambda: True)
    audio_targets.set_music_pref("phone")
    assert cli._resolve_music_where("default") == "phone"
    assert cli._resolve_music_where("") == "phone"
    assert cli._resolve_music_where("rooms") == "rooms"     # explicit wins
    audio_targets.set_music_pref(None)
    monkeypatch.setenv("MEDIA_MUSIC_DEFAULT_TARGET", "rooms")
    assert cli._resolve_music_where("default") == "rooms"


def test_music_pref_refuses_what_is_not_configured(monkeypatch):
    from agent_media_core.sinks import music_app

    monkeypatch.setattr(music_app, "configured", lambda: False)
    with pytest.raises(ValueError):
        audio_targets.set_music_pref("app")
    with pytest.raises(ValueError):
        audio_targets.set_music_pref("banana")
    assert audio_targets.set_music_pref("local") == "rooms"


def test_music_now_is_known_only_for_the_track_we_sent():
    audio_targets.note_music_played("phone", "yt:abc")
    assert audio_targets.music_now({"uri": "yt:abc"}) == "phone"
    assert audio_targets.music_now({"uri": "yt:other"}) is None
    assert audio_targets.music_now(None) is None


def test_music_block_shape():
    b = audio_targets.music_block(None)
    assert set(b) == {"current", "next", "overridden", "options"}
    assert [o["name"] for o in b["options"]] == ["auto", "rooms", "phone", "app"]
    assert b["current"] is None and b["next"] == "default" and b["overridden"] is False
