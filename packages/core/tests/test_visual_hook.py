"""Opt-in visual accompaniment: the Stop hook hands the raw reply to
`media-visual` (fire-and-forget, only when the optional binary exists)."""

from agent_media_core.intake import _visual
from agent_media_core.intake import hook_claude_code as H


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MEDIA_SPEECH_VISUAL", raising=False)
    assert _visual.visual_enabled() is False


def test_enabled_when_set(monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_VISUAL", "1")
    assert _visual.visual_enabled() is True


def test_min_chars_default_and_override(monkeypatch):
    monkeypatch.delenv("MEDIA_VISUAL_MIN_CHARS", raising=False)
    assert _visual.visual_min_chars() == _visual.DEFAULT_MIN_CHARS
    monkeypatch.setenv("MEDIA_VISUAL_MIN_CHARS", "50")
    assert _visual.visual_min_chars() == 50
    monkeypatch.setenv("MEDIA_VISUAL_MIN_CHARS", "not-an-int")
    assert _visual.visual_min_chars() == _visual.DEFAULT_MIN_CHARS


def test_caption_short_text_passes_through():
    assert _visual._caption("A short spoken line.") == "A short spoken line."


def test_caption_cuts_at_word_boundary():
    long = "word " * 60
    cap = _visual._caption(long)
    assert len(cap) <= _visual.CAPTION_MAX + 1  # +1 for the ellipsis
    assert cap.endswith("…")
    assert not cap[:-1].endswith(" ")


def test_spawn_no_binary_is_silent_noop(monkeypatch):
    monkeypatch.setattr(_visual.shutil, "which", lambda name: None)

    def _boom(*a, **k):
        raise AssertionError("Popen must not be called without the binary")

    monkeypatch.setattr(_visual.subprocess, "Popen", _boom)
    _visual.spawn_visual("raw reply", "spoken text")  # must not raise


def test_spawn_passes_caption_and_raw_detached(monkeypatch):
    seen = {}
    monkeypatch.setattr(_visual.shutil, "which",
                        lambda name: "/usr/bin/media-visual")

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs

    monkeypatch.setattr(_visual.subprocess, "Popen", fake_popen)
    _visual.spawn_visual("the raw **reply**", "the spoken text")
    assert seen["argv"][0] == "/usr/bin/media-visual"
    assert seen["argv"][1:3] == ["--caption", "the spoken text"]
    assert seen["argv"][3] == "the raw **reply**"
    assert seen["kwargs"]["start_new_session"] is True


def test_spawn_oserror_is_swallowed(monkeypatch):
    monkeypatch.setattr(_visual.shutil, "which",
                        lambda name: "/usr/bin/media-visual")

    def _boom(*a, **k):
        raise OSError("fork failed")

    monkeypatch.setattr(_visual.subprocess, "Popen", _boom)
    _visual.spawn_visual("raw", "spoken")  # must not raise


# --- Stop-path wiring: the carry into metadata and the pop in _play_now ------

def _capture_submit(monkeypatch):
    seen = {}

    def fake_submit(event, **_):
        seen["event"] = event
        return "rid-1"

    monkeypatch.setattr(H, "submit_event", fake_submit)
    # Run the detached path inline so the mock is observable (see
    # test_hook_session.py).
    monkeypatch.setenv("MEDIA_HOOK_NO_DETACH", "1")
    return seen


def _stop_payload(tmp_path, raw_text):
    return {"last_assistant_message": raw_text,
            "transcript_path": str(tmp_path / "t.jsonl"),
            "session_id": "sess-visual"}


def test_stop_spawns_visual_and_strips_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MEDIA_SPEECH_VISUAL", "1")
    monkeypatch.setenv("MEDIA_VISUAL_MIN_CHARS", "10")
    seen = _capture_submit(monkeypatch)
    monkeypatch.setattr(H, "_dedup_seen", lambda *a, **k: False)
    monkeypatch.setattr(H, "_session_name", lambda: "")
    spawned = {}
    monkeypatch.setattr(
        _visual, "spawn_visual",
        lambda raw, spoken, session="", hint="": spawned.update(
            raw=raw, spoken=spoken, session=session, hint=hint))

    raw = "A reply long enough to illustrate with a picture."
    assert H._handle_stop(_stop_payload(tmp_path, raw)) == 0
    assert spawned["raw"] == raw
    # The session id keys the canvas's scene-continuity memory.
    assert spawned["session"] == "sess-visual"
    # The raw reply must never ride into submit_event's metadata.
    assert "visual_raw" not in (seen["event"].metadata or {})


def test_stop_below_min_chars_does_not_spawn(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MEDIA_SPEECH_VISUAL", "1")
    monkeypatch.setenv("MEDIA_VISUAL_MIN_CHARS", "500")
    _capture_submit(monkeypatch)
    monkeypatch.setattr(H, "_dedup_seen", lambda *a, **k: False)
    monkeypatch.setattr(H, "_session_name", lambda: "")
    monkeypatch.setattr(_visual, "spawn_visual",
                        lambda *a: (_ for _ in ()).throw(AssertionError("spawned")))

    assert H._handle_stop(_stop_payload(tmp_path, "short reply")) == 0


def test_stop_disabled_does_not_spawn(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MEDIA_SPEECH_VISUAL", raising=False)
    _capture_submit(monkeypatch)
    monkeypatch.setattr(H, "_dedup_seen", lambda *a, **k: False)
    monkeypatch.setattr(H, "_session_name", lambda: "")
    monkeypatch.setattr(_visual, "spawn_visual",
                        lambda *a: (_ for _ in ()).throw(AssertionError("spawned")))

    assert H._handle_stop(
        _stop_payload(tmp_path, "A reply long enough to illustrate.")) == 0


def test_deduped_reply_does_not_spawn(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MEDIA_SPEECH_VISUAL", "1")
    monkeypatch.setenv("MEDIA_VISUAL_MIN_CHARS", "10")
    _capture_submit(monkeypatch)
    monkeypatch.setattr(H, "_dedup_seen", lambda *a, **k: True)
    monkeypatch.setattr(H, "_session_name", lambda: "")
    monkeypatch.setattr(_visual, "spawn_visual",
                        lambda *a: (_ for _ in ()).throw(AssertionError("spawned")))

    assert H._handle_stop(
        _stop_payload(tmp_path, "A duplicate reply that was already spoken.")) == 0
