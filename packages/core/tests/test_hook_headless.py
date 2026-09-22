"""The Claude Code hook in a headless session (the server's media-sessiond).

A headless session has no pane. sessiond marks it `MEDIA_SOURCE_KIND=headless`
and names the workspace a pane would have opened in (`MEDIA_SOURCE_WORKSPACE`),
so its replies are spoken exactly like a pane session's: the session id on
the event, the same per-session voice, and filed under the same workspace
(`source_tmux_session`) — with `source_pane` empty, and no tmux asked.
"""

from agent_media_core.intake import hook_claude_code as H


def _stop(tmp_path, monkeypatch, env: dict) -> dict:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    for k in ("MEDIA_SOURCE_KIND", "MEDIA_SOURCE_WORKSPACE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    seen: dict = {}
    monkeypatch.setattr(H, "submit_event", lambda event, **_: seen.setdefault("event", event))
    monkeypatch.setenv("MEDIA_HOOK_NO_DETACH", "1")
    monkeypatch.setattr(H, "_latest_assistant_text", lambda tp: "All done.")
    monkeypatch.setattr(H, "_dedup_seen", lambda *a, **k: False)

    def no_tmux(argv, *a, **k):
        raise AssertionError(f"asked tmux {argv} with no pane")

    monkeypatch.setattr(H, "_tmux", no_tmux)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    assert H._handle_stop({"transcript_path": str(transcript), "session_id": "hl-sess"}) == 0
    return seen


def test_a_headless_reply_is_spoken_under_its_workspace(tmp_path, monkeypatch):
    seen = _stop(tmp_path, monkeypatch, {"MEDIA_SOURCE_KIND": "headless",
                                         "MEDIA_SOURCE_WORKSPACE": "p-agent-media"})
    ev = seen["event"]
    assert ev.metadata["session"] == "hl-sess"
    assert ev.metadata["tmux"] == "p-agent-media"
    assert "pane" not in ev.metadata
    # The voice a pane in that tmux session would have had.
    assert ev.voice == H._voice_for_session("p-agent-media")


def test_the_workspace_needs_the_headless_marker(tmp_path, monkeypatch):
    seen = _stop(tmp_path, monkeypatch, {"MEDIA_SOURCE_WORKSPACE": "p-agent-media"})
    ev = seen["event"]
    assert ev.metadata["session"] == "hl-sess"
    assert "tmux" not in ev.metadata
    assert ev.voice is None


def test_no_pane_and_no_marker_is_unchanged(tmp_path, monkeypatch):
    seen = _stop(tmp_path, monkeypatch, {})
    assert "tmux" not in seen["event"].metadata and "pane" not in seen["event"].metadata


def test_the_listener_turn_carries_the_workspace(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("MEDIA_SOURCE_KIND", "headless")
    monkeypatch.setenv("MEDIA_SOURCE_WORKSPACE", "amux-scratch")
    assert H._session_name() == "amux-scratch"
    assert H._source_place() == {"tmux": "amux-scratch"}
