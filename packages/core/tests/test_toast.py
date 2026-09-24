"""The desk toast: a reply from a pane you are not looking at waits for you."""

from __future__ import annotations

import os
import time

from agent_media_core.intake import toast
from agent_media_core.types import Event, Priority, Source


def _event(text="hello there"):
    return Event(text=text, source=Source.CLAUDE_CODE, priority=Priority.NORMAL,
                 voice="af_heart", metadata={"kind": "stop", "session": "s1"})


def _fake_tmux(monkeypatch, clients=(("/dev/pts/1", "%1", 5),)):
    """clients: (name, pane it shows, seconds since last keystroke)."""
    shown = []

    def tmux(args):
        if args[0] == "list-clients":
            now = int(time.time())
            return "\n".join(f"{now - age} {name} {pane}"
                             for name, pane, age in clients)
        if args[0] == "display-message" and "-c" in args:
            shown.append(args[-1])
            return ""
        return "main:2 agent-media"

    monkeypatch.setattr(toast, "_tmux", tmux)
    return shown


def test_gate_off_never_holds(monkeypatch):
    _fake_tmux(monkeypatch)
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.delenv("MEDIA_TOAST_GATE", raising=False)
    assert not toast.should_hold()


def test_holds_only_off_pane_with_someone_at_the_desk(monkeypatch):
    monkeypatch.setenv("MEDIA_TOAST_GATE", "1")
    monkeypatch.setenv("TMUX_PANE", "%3")
    _fake_tmux(monkeypatch)
    assert toast.should_hold()                        # looking at %1
    _fake_tmux(monkeypatch, clients=(("/dev/pts/1", "%3", 5),))
    assert not toast.should_hold()                    # looking at this pane
    _fake_tmux(monkeypatch, clients=())
    assert not toast.should_hold()                    # nobody attached


def test_a_stale_client_is_not_someone_at_the_desk(monkeypatch):
    monkeypatch.setenv("MEDIA_TOAST_GATE", "1")
    monkeypatch.setenv("TMUX_PANE", "%3")
    _fake_tmux(monkeypatch, clients=(("/dev/pts/1", "%1", 86400),))
    assert not toast.should_hold()                    # away: the phone lane
    # The freshest client decides which pane is "the one you are on".
    _fake_tmux(monkeypatch, clients=(("/dev/pts/1", "%1", 86400),
                                     ("/dev/pts/2", "%3", 10)))
    assert not toast.should_hold()


def test_outside_tmux_never_holds(monkeypatch):
    monkeypatch.setenv("MEDIA_TOAST_GATE", "1")
    monkeypatch.delenv("TMUX_PANE", raising=False)
    _fake_tmux(monkeypatch)
    assert not toast.should_hold()


def test_hold_archives_unplayed_then_play_replays_the_row(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%3")
    shown = _fake_tmux(monkeypatch)
    rendered, replayed = [], []
    import agent_media_core.intake.hook_claude_code as hook
    import agent_media_core.cli as cli
    monkeypatch.setattr(hook, "_play_detached", rendered.append)
    monkeypatch.setattr(cli, "main", lambda argv: replayed.append(argv) or 0)
    rows = {}
    monkeypatch.setattr(toast, "_row_for", lambda key: rows.get(key))

    toast.hold(_event("first"))
    toast.hold(_event("second"))
    # Rendered at once, marked held so submit archives it unplayed.
    assert [e.text for e in rendered] == ["first", "second"]
    assert all(e.metadata["held"] for e in rendered)
    assert "reply ready (+1 more)" in shown[-1]
    assert [r["text"] for r in toast.listing()] == ["first", "second"]

    import hashlib
    rows[hashlib.sha1(b"second").hexdigest()] = 42
    assert toast.play() == 0
    assert replayed == [["replay", "--id", "42"]]
    assert "reply ready —" in shown[-1]   # re-toast for the one still waiting

    assert toast.dismiss() == 0
    assert toast.listing() == []
    assert toast.play() == 1


def test_play_gives_up_on_a_reply_that_never_rendered(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.setenv("MEDIA_TOAST_RENDER_WAIT_S", "0")
    shown = _fake_tmux(monkeypatch)
    import agent_media_core.intake.hook_claude_code as hook
    monkeypatch.setattr(hook, "_play_detached", lambda e: None)
    monkeypatch.setattr(toast, "_row_for", lambda key: None)
    toast.hold(_event())
    assert toast.play() == 1
    assert "never rendered" in shown[-1]


def test_expired_reply_is_dropped(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.setenv("MEDIA_TOAST_TTL", "60")
    _fake_tmux(monkeypatch)
    import agent_media_core.intake.hook_claude_code as hook
    monkeypatch.setattr(hook, "_play_detached", lambda e: None)
    toast.hold(_event())
    [path] = list(toast._pending_dir().glob("*.json"))
    old = time.time() - 120
    os.utime(path, (old, old))
    assert toast.listing() == []
    assert not path.exists()


def test_a_held_row_is_unheard_until_marked_heard():
    from agent_media_core import session_feed
    from agent_media_core.state import StateStore

    st = StateStore()
    rid = st.add_history(sink="speech", uri="/nonexistent.mp3", started_at=time.time(),
                         target="phone", source="claude-code", text="waiting for you",
                         extras={"source_session": "s1", "held": True, "muted": True})
    assert st.mark_heard(rid) is True
    assert st.history_row(rid)["extras"]["heard"] is True
    assert st.mark_heard(rid) is False          # already heard
    plain = st.add_history(sink="speech", uri="/x.mp3", started_at=time.time(),
                           target="phone", source="claude-code", text="said",
                           extras={"source_session": "s1", "muted": True})
    assert st.mark_heard(plain) is False        # muted on purpose: not ours
    assert session_feed.Turn(at=0, text="x").unheard is False


def test_the_feed_and_the_log_carry_unheard(tmp_path):
    from agent_media_core import book_tracks, session_feed
    from agent_media_core.state import StateStore

    clip = tmp_path / "c.mp3"
    clip.write_bytes(b"\0")
    st = StateStore()
    base = {"source_session": "s9", "clip_uris": [str(clip)], "clip_durations_s": [1.0]}
    st.add_history(sink="speech", uri=str(clip), started_at=100.0, target="phone",
                   source="claude-code", text="heard one", extras=dict(base))
    rid = st.add_history(sink="speech", uri=str(clip), started_at=200.0, target="phone",
                         source="claude-code", text="held one",
                         extras={**base, "held": True, "muted": True})
    turns = session_feed.turns("s9", store=st)
    assert [t.unheard for t in turns] == [False, True]
    lines = book_tracks.conversation_log("s9", tmp_path, positions=False)
    assert [l.get("unheard", False) for l in lines] == [False, True]
    st.mark_heard(rid)
    assert [t.unheard for t in session_feed.turns("s9", store=st)] == [False, False]
