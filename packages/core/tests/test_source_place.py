"""Who the submitter believes about where a clip was said.

The pane's tmux session and window name are asked of tmux at submit time, and
by then the pane may be gone: a reply is rendered and queued before a word of
it is spoken, and the turn that ends a conversation is followed by the window
closing. So the hook — which runs in the pane, at the moment the turn ends —
sends both names along, and they win.
"""

from agent_media_core.intake import submit as S


def _tmux_would_say(monkeypatch, session, window):
    monkeypatch.setattr(S, "_tmux_session_for_pane", lambda pane: session)
    monkeypatch.setattr(S, "_tmux_window_for_pane", lambda pane: window)


def test_what_the_hook_saw_beats_what_tmux_still_remembers(monkeypatch):
    _tmux_would_say(monkeypatch, "", "")      # the pane has closed since
    assert S._source_place({"tmux": "work", "window": "the ball"}, "%155") \
        == ("work", "the ball")


def test_a_caller_that_knows_nothing_still_gets_asked_for(monkeypatch):
    # `media say` from a shell knows its pane and no more.
    _tmux_would_say(monkeypatch, "work", "the ball")
    assert S._source_place({}, "%155") == ("work", "the ball")
    assert S._source_place(None, "%155") == ("work", "the ball")


def test_each_name_falls_back_on_its_own(monkeypatch):
    _tmux_would_say(monkeypatch, "asked", "found")
    assert S._source_place({"tmux": "told"}, "%1") == ("told", "found")
    assert S._source_place({"window": "told"}, "%1") == ("asked", "told")


def test_blank_is_not_an_answer(monkeypatch):
    # An empty string in the metadata is a hook that had nothing to say, not a
    # pane that lives nowhere.
    _tmux_would_say(monkeypatch, "work", "the ball")
    assert S._source_place({"tmux": "  ", "window": ""}, "%1") \
        == ("work", "the ball")


def test_a_paneless_agent_is_still_in_its_workspace(monkeypatch):
    # Headless: no pane, so tmux knows nothing — but the session was started
    # with its workspace in the environment, and the voice hangs off it.
    _tmux_would_say(monkeypatch, "", "")
    monkeypatch.setenv("MEDIA_SOURCE_WORKSPACE", "agent-media")
    assert S._source_place({}, "")[0] == "agent-media"
    assert S._source_place({"tmux": "told"}, "")[0] == "told"


def test_an_agent_speaking_mid_turn_belongs_to_its_conversation(monkeypatch):
    """`media say` inside a tool call: the hook's id is absent, the
    environment's is not, and a lead-in spoken before a question has to land
    in the same conversation as the question."""
    for var in ("MEDIA_SOURCE_SESSION", "CLAUDE_CODE_SESSION_ID", "MEDIA_SESSIOND_SESSION"):
        monkeypatch.delenv(var, raising=False)
    assert S._source_session({}) == ""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    assert S._source_session(None) == "11111111-2222-3333-4444-555555555555"
    monkeypatch.setenv("MEDIA_SESSIOND_SESSION", "99999999-2222-3333-4444-555555555555")
    assert S._source_session({}) == "11111111-2222-3333-4444-555555555555"
    # What the hook was handed still wins over anything in the environment.
    assert S._source_session({"session": "aaaaaaaa-2222-3333-4444-555555555555"}) \
        == "aaaaaaaa-2222-3333-4444-555555555555"


def test_a_shell_outside_an_agent_files_under_nobody(monkeypatch):
    for var in ("MEDIA_SOURCE_SESSION", "CLAUDE_CODE_SESSION_ID", "MEDIA_SESSIOND_SESSION"):
        monkeypatch.delenv(var, raising=False)
    assert S._source_session({"session": ""}) == ""


def test_an_agents_aside_is_spoken_in_the_conversations_voice(monkeypatch):
    """`media say` inside a turn is the same speaker as the reply that
    follows it, so it takes the workspace's voice rather than the default."""
    import argparse

    from agent_media_core import cli

    said = {}
    monkeypatch.setattr("agent_media_core.intake.submit.submit_event",
                        lambda event: said.update(voice=event.voice, text=event.text))
    monkeypatch.setenv("MEDIA_SESSION_VOICE_MAP", "agent-media=en-NZ-MollyNeural")
    monkeypatch.setenv("MEDIA_SOURCE_WORKSPACE", "agent-media")
    cli.cmd_say(argparse.Namespace(text="a word before the question", urgent=False,
                                   supersede=False, alert=False))
    assert said == {"voice": "en-NZ-MollyNeural", "text": "a word before the question"}
    # Outside an agent there is no workspace, and nothing is pinned.
    said.clear()
    monkeypatch.delenv("MEDIA_SOURCE_WORKSPACE")
    cli.cmd_say(argparse.Namespace(text="from a shell", urgent=False,
                                   supersede=False, alert=False))
    assert said["voice"] is None
