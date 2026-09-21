"""Two things a new chat from the phone got wrong on 2026-09-22.

The message sat unsent in a phone-width pane because only its first words
were checked, and those had scrolled out of the composer; and the thread was
listed as "Claude Code", which is how Claude names a session it has not
named yet.
"""

from agent_media_server import panes, send, sessions


def _screen(monkeypatch, text):
    monkeypatch.setattr(send.time, "sleep", lambda s: None)
    monkeypatch.setattr(sessions, "_capture_pane", lambda pane: text)
    monkeypatch.setattr(panes, "classify", lambda cap, agent="claude": "input")


def test_a_wrapped_message_whose_start_scrolled_away_is_still_unsent(monkeypatch):
    msg = "I want to try a new feature? maybe something that uses the paragtd repo?"
    _screen(monkeypatch, "── \n❯ feature? maybe something that\n  uses the paragtd repo?\n──")
    flat = " ".join(msg.split())
    assert send._unsent("%1", (flat[:24], flat[-24:]), "claude", 0)
    # The old single mark would have said "gone".
    assert not send._unsent("%1", flat[:24], "claude", 0)


def test_an_empty_composer_is_sent(monkeypatch):
    _screen(monkeypatch, "── \n❯ \n──")
    assert not send._unsent("%1", ("I want to try a new", "uses the paragtd repo?"), "claude", 0)


def test_an_unnamed_claude_session_takes_its_first_message(monkeypatch, tmp_path):
    proj = tmp_path / ".claude" / "projects" / "-x"
    proj.mkdir(parents=True)
    sid = "cb51c3c5-4835-40fa-acf1-4ca6176a4eb4"
    (proj / f"{sid}.jsonl").write_text(
        '{"type":"user","isMeta":true,"message":{"content":"<command-name>/x</command-name>"}}\n'
        '{"type":"user","message":{"content":"Build a feature that uses the paragtd repo"}}\n')
    monkeypatch.setenv("HOME", str(tmp_path))
    sessions._FIRST_PROMPT.clear()
    assert sessions._display_title(sid, "Claude Code") == "Build a feature that uses the paragtd repo"
    assert sessions._display_title(sid, "A real name") == "A real name"
    assert sessions._display_title("no-such", "Claude Code") == "Claude Code"
