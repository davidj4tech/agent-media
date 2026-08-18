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
