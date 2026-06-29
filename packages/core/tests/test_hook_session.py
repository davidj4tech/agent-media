"""The Claude Code hook forwards its session_id into the event metadata.

That id is what lets the popup's `goto-pane` resume a conversation whose
source pane has since been closed (`claude --resume <session>`).
"""

from agent_media_core.intake import hook_claude_code as H


def _capture_submit(monkeypatch):
    seen = {}

    def fake_submit(event, **_):
        seen["event"] = event
        return "rid-1"

    monkeypatch.setattr(H, "submit_event", fake_submit)
    # The Stop path detaches playback into a forked child where an in-process
    # mock can't be observed; run inline so the captured call is visible.
    monkeypatch.setenv("MEDIA_HOOK_NO_DETACH", "1")
    return seen


def test_stop_forwards_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    seen = _capture_submit(monkeypatch)
    monkeypatch.setattr(H, "_latest_assistant_text", lambda tp: "hello there")
    monkeypatch.setattr(H, "_dedup_seen", lambda *a, **k: False)
    monkeypatch.setattr(H, "_session_name", lambda: "")

    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    assert H._handle_stop({"transcript_path": str(transcript),
                           "session_id": "stop-sess"}) == 0
    assert (seen["event"].metadata or {}).get("session") == "stop-sess"


def test_notification_forwards_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    seen = _capture_submit(monkeypatch)
    monkeypatch.setattr(H, "_client_focused_recently", lambda within: False)
    monkeypatch.setattr(H, "_session_name", lambda: "")
    monkeypatch.setattr(H, "_notif_label", lambda sess: "")

    assert H._handle_notification({"message": "Claude is waiting",
                                   "session_id": "notif-sess"}) == 0
    assert (seen["event"].metadata or {}).get("session") == "notif-sess"


def test_missing_session_id_is_empty_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    seen = _capture_submit(monkeypatch)
    monkeypatch.setattr(H, "_latest_assistant_text", lambda tp: "hi")
    monkeypatch.setattr(H, "_dedup_seen", lambda *a, **k: False)
    monkeypatch.setattr(H, "_session_name", lambda: "")

    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    assert H._handle_stop({"transcript_path": str(transcript)}) == 0
    assert (seen["event"].metadata or {}).get("session") == ""
