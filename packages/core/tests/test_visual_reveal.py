"""[[visual:]]/[[reveal:]] markers: purposeful pictures, speech that holds."""

from agent_media_core.intake import _visual
from agent_media_core.intake import hook_claude_code as H


# --- marker extraction ---------------------------------------------------------

def test_no_marker_passthrough():
    clean, hint, pre, post = _visual.extract_visual_markers("plain reply")
    assert (clean, hint, pre, post) == ("plain reply", "", None, None)


def test_visual_marker_stripped_and_hint_captured():
    raw = "Before. [[visual: a labeled diagram of the pipeline]] After."
    clean, hint, pre, post = _visual.extract_visual_markers(raw)
    assert "[[" not in clean and "diagram of the pipeline" in hint
    assert pre is None and post is None


def test_reveal_marker_splits():
    raw = "Look at this. [[reveal: two boxes joined by an arrow]] As you can see."
    clean, hint, pre, post = _visual.extract_visual_markers(raw)
    assert hint == "two boxes joined by an arrow"
    assert pre.strip() == "Look at this."
    assert post.strip() == "As you can see."
    assert "[[" not in clean


def test_marker_case_and_whitespace():
    raw = "A [[ Reveal :  the thing\n  drawn large ]] B"
    clean, hint, pre, post = _visual.extract_visual_markers(raw)
    assert hint == "the thing drawn large"
    assert pre.strip() == "A" and post.strip() == "B"


def test_spawn_includes_hint(monkeypatch):
    seen = {}
    monkeypatch.setattr(_visual.shutil, "which",
                        lambda name: "/usr/bin/media-visual")
    monkeypatch.setattr(_visual.subprocess, "Popen",
                        lambda argv, **kw: seen.update(argv=argv))
    _visual.spawn_visual("raw", "spoken", "sess", hint="a diagram")
    i = seen["argv"].index("--hint")
    assert seen["argv"][i + 1] == "a diagram"


# --- the wait ---------------------------------------------------------------

def test_wait_returns_true_on_fresh_image(monkeypatch):
    class _R:
        def __init__(self, body): self._b = body
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(_visual.urllib.request, "urlopen",
                        lambda url, timeout: _R(b'{"t": 1000.0}'))
    assert _visual.wait_for_fresh_visual(999.0, timeout_s=3) is True


def test_wait_times_out_on_stale_image(monkeypatch):
    class _R:
        def read(self): return b'{"t": 5.0}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(_visual.urllib.request, "urlopen",
                        lambda url, timeout: _R())
    monkeypatch.setattr(_visual.time, "sleep", lambda s: None)
    assert _visual.wait_for_fresh_visual(999.0, timeout_s=1) is False


def test_wait_survives_unreachable_canvas(monkeypatch):
    def _boom(url, timeout):
        raise OSError("no canvas")

    monkeypatch.setattr(_visual.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(_visual.time, "sleep", lambda s: None)
    assert _visual.wait_for_fresh_visual(0.0, timeout_s=1) is False


# --- the stop path end to end ---------------------------------------------------

def _arm(tmp_path, monkeypatch):
    submitted = []

    def fake_submit(event, **_):
        submitted.append(event)
        return f"rid-{len(submitted)}"

    monkeypatch.setattr(H, "submit_event", fake_submit)
    monkeypatch.setenv("MEDIA_HOOK_NO_DETACH", "1")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MEDIA_SPEECH_VISUAL", "1")
    monkeypatch.setattr(H, "_dedup_seen", lambda *a, **k: False)
    monkeypatch.setattr(H, "_session_name", lambda: "")
    return submitted


REVEAL_RAW = ("Here is the layout I promised. "
              "[[reveal: three labeled boxes: intake, route, sinks]] "
              "As you can see, intake feeds route.")


def test_reveal_speaks_two_parts_around_the_wait(tmp_path, monkeypatch):
    submitted = _arm(tmp_path, monkeypatch)
    calls = {"spawn": 0, "wait": 0}
    monkeypatch.setattr(_visual, "spawn_visual",
                        lambda *a, **k: calls.__setitem__("spawn", calls["spawn"] + 1))

    def fake_wait(after, timeout_s=None):
        calls["wait"] += 1
        # Part one must already be enqueued when the hold starts.
        assert len(submitted) == 1
        return True

    monkeypatch.setattr(_visual, "wait_for_fresh_visual", fake_wait)
    assert H._handle_stop({"last_assistant_message": REVEAL_RAW,
                           "transcript_path": str(tmp_path / "t.jsonl"),
                           "session_id": "s1"}) == 0
    assert calls == {"spawn": 1, "wait": 1}
    assert len(submitted) == 2
    assert "layout I promised" in submitted[0].text
    assert "intake feeds route" in submitted[1].text
    for e in submitted:
        assert "[[" not in e.text
        assert (e.metadata or {}).get("session") == "s1"
    assert submitted[0].metadata["dedup_key"] != submitted[1].metadata["dedup_key"]


def test_reveal_timeout_still_speaks_part_two(tmp_path, monkeypatch):
    submitted = _arm(tmp_path, monkeypatch)
    monkeypatch.setattr(_visual, "spawn_visual", lambda *a, **k: None)
    monkeypatch.setattr(_visual, "wait_for_fresh_visual",
                        lambda *a, **k: False)
    H._handle_stop({"last_assistant_message": REVEAL_RAW,
                    "transcript_path": str(tmp_path / "t.jsonl"),
                    "session_id": "s1"})
    assert len(submitted) == 2


def test_visual_marker_no_split_single_event(tmp_path, monkeypatch):
    submitted = _arm(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(_visual, "spawn_visual",
                        lambda raw, spoken, session="", hint="", key="":
                        seen.update(hint=hint))
    monkeypatch.setattr(_visual, "wait_for_fresh_visual",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not wait")))
    raw = "Short. [[visual: a map of the rooms]] Done."
    H._handle_stop({"last_assistant_message": raw,
                    "transcript_path": str(tmp_path / "t.jsonl"),
                    "session_id": "s1"})
    assert len(submitted) == 1 and "[[" not in submitted[0].text
    assert seen["hint"] == "a map of the rooms"


def test_hint_bypasses_min_chars(tmp_path, monkeypatch):
    _arm(tmp_path, monkeypatch)
    monkeypatch.setenv("MEDIA_VISUAL_MIN_CHARS", "5000")
    seen = {}
    monkeypatch.setattr(_visual, "spawn_visual",
                        lambda raw, spoken, session="", hint="", key="":
                        seen.update(hint=hint))
    H._handle_stop({"last_assistant_message":
                    "Tiny. [[visual: one bright square]] End.",
                    "transcript_path": str(tmp_path / "t.jsonl"),
                    "session_id": "s1"})
    assert seen["hint"] == "one bright square"


def test_markers_stripped_even_when_visual_disabled(tmp_path, monkeypatch):
    submitted = _arm(tmp_path, monkeypatch)
    monkeypatch.delenv("MEDIA_SPEECH_VISUAL", raising=False)
    monkeypatch.setattr(_visual, "spawn_visual",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("disabled")))
    H._handle_stop({"last_assistant_message": REVEAL_RAW,
                    "transcript_path": str(tmp_path / "t.jsonl"),
                    "session_id": "s1"})
    assert len(submitted) == 1 and "[[" not in submitted[0].text


def test_visual_flag_rides_event_metadata(tmp_path, monkeypatch):
    submitted = _arm(tmp_path, monkeypatch)
    monkeypatch.setattr(_visual, "spawn_visual", lambda *a, **k: None)
    monkeypatch.setattr(_visual, "wait_for_fresh_visual", lambda *a, **k: True)
    H._handle_stop({"last_assistant_message": REVEAL_RAW,
                    "transcript_path": str(tmp_path / "t.jsonl"),
                    "session_id": "s1"})
    # Both reveal parts are marked so the status bar / popup can show ▣.
    assert all(e.metadata.get("visual") == "reveal" for e in submitted)


def test_ambient_reply_has_no_visual_flag(tmp_path, monkeypatch):
    submitted = _arm(tmp_path, monkeypatch)
    monkeypatch.setenv("MEDIA_VISUAL_MIN_CHARS", "10")
    monkeypatch.setattr(_visual, "spawn_visual", lambda *a, **k: None)
    H._handle_stop({"last_assistant_message":
                    "A plain reply long enough for an ambient picture.",
                    "transcript_path": str(tmp_path / "t.jsonl"),
                    "session_id": "s1"})
    assert submitted[0].metadata.get("visual") is None
