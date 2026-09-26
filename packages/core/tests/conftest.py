import os

import pytest


@pytest.fixture(autouse=True)
def _no_live_phone_backend(monkeypatch):
    """Keep the suite away from the real phone player.

    `media music` transport routes to the phone-local backend whenever
    MEDIA_MUSIC_LOCAL_ENDPOINT is set (the cli module loads it from
    ~/.config/agent-media.env at import) and the phone's mpv has a track
    loaded — so on a dev box with music playing, an un-scrubbed test run
    would seek/pause the user's actual playback.

    Since the `app` target (music in Sasonica) the router prefers an even
    nearer backend, chosen by MEDIA_PHONE_PLAYER_URL — and it is tried
    FIRST, so scrubbing only the mpv endpoint stopped covering this. It
    was not hypothetical: `media music seek` in the suite reached the
    phone's player for real, and the two music-seek tests failed here
    while passing in CI, which is what an inherited-config hazard looks
    like from the outside. Scrubbed by prefix so a per-target key
    (..._URL_APP) goes too; a test that wants a player sets its own.
    """
    monkeypatch.delenv("MEDIA_MUSIC_LOCAL_ENDPOINT", raising=False)
    for key in [k for k in os.environ if k.startswith("MEDIA_PHONE_PLAYER_URL")]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _no_remote_say(monkeypatch):
    """Keep the suite on the local render path.

    MEDIA_REMOTE_SAY_CMD replaces rendering entirely: submit_event hands the
    whole reply to another host and returns. Any test asserting on clips,
    history extras or playback then exercises a branch it never meant to, and
    fails in a way that points at the code rather than at the config it
    inherited. That config is not hypothetical — a host that speaks through a
    remote hub (or has media-lane switching lanes by network) sets this
    variable in ~/.config/agent-media.env, so the suite would pass or fail
    depending on which room the developer is standing in.

    A test that wants the remote path should set it explicitly.

    The per-target keys have to go too, and by prefix rather than by name:
    the lane is now chosen by MEDIA_REMOTE_SAY_CMD_<TARGET>, so media-lane
    writes ..._PHONE and scrubbing only the bare name would leave exactly the
    inherited-config hazard above — passing or failing by which room the
    developer is standing in — while looking as though it were handled.
    """
    for key in [k for k in os.environ if k.startswith("MEDIA_REMOTE_SAY_CMD")]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _no_inherited_mailbox(monkeypatch):
    """Same hazard as above, and it was live: the converse doorbell's mailbox.

    `doorbell.post` reads MEDIA_CONVERSE_MAILBOX and does nothing when it is
    unset, so two tests that asserted on the relay-msg argv passed here and
    failed in CI — this developer's ~/.config/agent-media.env names the box
    (`cece`) and the sender (`sam`), and something earlier in the suite loads
    that file into os.environ for real. In isolation the same two tests failed
    locally too, which is the tell: they were reading config, not fixtures.

    Scrubbed by prefix like the remote-say keys, and deliberately not
    MEDIA_CONVERSE_NOTIFY, which the notification tests set for themselves.
    """
    for key in [k for k in os.environ if k.startswith("MEDIA_CONVERSE_MAILBOX")]:
        monkeypatch.delenv(key, raising=False)


# Keep test transitions out of the production floor history.
#
# Same hazard as the fixtures in this file — real config, real side effect —
# but the damage lands somewhere durable rather than on the developer's desk.
# Every speech-hold and every armed rendezvous in this suite spawns
# `relay-floor.sh publish`, so a run deposits rows like `speech/pane7 hold 120`
# and `input/sam arm 5` into the live D1 table, permanently and
# indistinguishably from real ones. That table exists for exactly one question
# — whether the per-owner holds have started colliding now that there are three
# of us — and it was added (tmux-relay migrations/0008) precisely because the
# local markers are reaped and that question had no answer on 2026-08-11. A
# history salted with fixture collisions cannot answer it either, which makes
# the mirror worse than useless: it looks authoritative while lying.
#
# Set at import rather than by an autouse monkeypatch fixture, which is what
# this was first written as and which leaked. `_armed()` in the rendezvous
# tests runs the `with Rendezvous(...)` block on a thread, and the tests that
# assert a *refusal* never join it — so `__exit__`, and its `_mirror("disarm")`,
# can fire after teardown has already restored the variable. That race let
# roughly one row per full run through: rare enough to look like it worked.
# A process-wide assignment has no restore window for the thread to land in.
#
# A test that wants to assert on the mirror should monkeypatch it back on.
os.environ["MEDIA_FLOOR_MIRROR"] = "0"


@pytest.fixture(autouse=True)
def _no_live_speech_token(monkeypatch, tmp_path):
    """Keep the suite off the live speech token and its followers.

    A replay now takes the playback token (`speech-playback.lock`), registers
    in `speech-waiters/` and stops any earlier replay's follower by the pid in
    `replay-track.pid` — all under the state dir. A test that replays without
    isolating that dir would queue behind (or make yield) whatever reply is
    speaking on the developer's phone, and could SIGTERM a real replay's
    follower. So every test starts with its own state dir; a test that points
    XDG_STATE_HOME somewhere itself still wins, being set after this.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    # opencode's conversations live under XDG_DATA_HOME (harnesses.opencode_db);
    # unset, a test that lists every harness's sessions would read this
    # machine's real ones.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


@pytest.fixture(autouse=True)
def _no_ringer_reads(monkeypatch):
    """Keep the suite from asking the real phone whether it is on silent.

    Every earcon now reads the ringer verdict for a phone target, over the
    broker socket the shell's config names — a real round trip to p8a from a
    test about tones, and a flaky one on a phone that happens to be silenced.
    No target is ringer-gated unless a test says so (test_ringer_gate does).
    """
    monkeypatch.setenv("MEDIA_RINGER_TARGET", "")


@pytest.fixture(autouse=True)
def _no_follow_pane(monkeypatch):
    """Keep the suite out of the developer's terminal.

    Speaking with auto-highlight on now opens the follow-along pane
    (`ensure_follow_view`), and the suite is very often run from inside the
    tmux session it would open it in — so an un-scrubbed run would split panes
    and add windows under the person watching the tests. Same hazard as the
    phone backend above: real config, real side effect, nothing to do with what
    the test is asserting. A test that wants the coupling sets this itself.
    """
    monkeypatch.setenv("MEDIA_FOLLOW_AUTO", "0")


@pytest.fixture(autouse=True)
def _no_listener_audio_choice(monkeypatch, tmp_path):
    """Keep the listener's own speech/music choice out of the suite.

    `media speech-target` (and the app's picker) writes a file in the real
    state dir that wins over MEDIA_SPEECH_DEFAULT_TARGET for every new reply.
    This suite does not isolate the state dir as a whole, so without this a
    developer who had moved speech to the rooms would see every test that
    asserts on the env default's target fail — or, worse, pass for the wrong
    reason. The files go to a per-test directory instead; a test that wants
    a choice writes it there through audio_targets.
    """
    from agent_media_core import audio_targets

    d = tmp_path / "audio-choice"
    monkeypatch.setattr(audio_targets, "_path", lambda name: d / name)


@pytest.fixture(autouse=True)
def _no_followup_calls(monkeypatch):
    """The Stop hook's follow-up is a gateway call on a thread; a hook test
    that runs the detached path would otherwise make it for real (and file
    the answer in the developer's own state dir). Tests of the follow-up
    itself re-enable it explicitly."""
    monkeypatch.setenv("MEDIA_FOLLOWUP", "0")


@pytest.fixture(autouse=True)
def _david_layout(monkeypatch):
    """The layout (agent_media_core/layout.py) pinned to David's, which is
    what these tests were written against: detection reads the real HOME
    (~/.amux, ~/.claude/settings.json), so unpinned they would pass on red5
    and fail anywhere else. test_layout.py unpins it where it needs to."""
    monkeypatch.setenv("MEDIA_LAYOUT", "projects-per-tmux-session")


@pytest.fixture(autouse=True)
def _not_headless(monkeypatch):
    """Keep a headless run's own identity out of the suite.

    A session the server started from the phone (media-sessiond) carries
    MEDIA_SOURCE_KIND=headless and its workspace, and the hook honours both —
    so running the suite from such a session filed every "outside tmux" stop
    under that workspace. A test that wants a headless session sets them.
    """
    monkeypatch.delenv("MEDIA_SOURCE_KIND", raising=False)
    monkeypatch.delenv("MEDIA_SOURCE_WORKSPACE", raising=False)


@pytest.fixture(autouse=True)
def _no_earcons(monkeypatch):
    """No tones from the suite (agent_media_core/earcons.py).

    An earcon is a real clip sent to a real player: a test that stops speech
    through the real SinkSpeech, or holds a reply behind the toast, would tick
    or chime on whatever broker the developer's env points at — the phone in
    his pocket, usually. Same hazard as the fixtures above. The earcon tests
    turn them back on for themselves, against a recording sink.
    """
    monkeypatch.setenv("MEDIA_EARCONS", "0")
