"""Prompt shaping (fresh vs evolving) and the multi-canvas push."""

import json

from agent_media_visual import cli, generate, state


def _capture_chat(monkeypatch, reply="a shaped scene"):
    seen = {}

    def fake_chat(system_prompt, user_text, timeout):
        seen["system"] = system_prompt
        seen["user"] = user_text
        return reply

    monkeypatch.setattr(generate, "_gateway_chat", fake_chat)
    return seen


def test_fresh_session_uses_shaper(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    seen = _capture_chat(monkeypatch)
    prompt, used = generate.shape_prompt("the reply", session="s1")
    assert used and seen["system"] == generate.PROMPT_SHAPER
    assert prompt.startswith("a shaped scene")


def test_next_reply_evolves_previous_scene(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    seen = _capture_chat(monkeypatch, reply="the scene, evolved")
    state.save_scene("s1", "a lighthouse at dusk")
    prompt, used = generate.shape_prompt("the second reply", session="s1")
    assert used and seen["system"] == generate.PROMPT_EVOLVER
    assert "a lighthouse at dusk" in seen["user"]
    assert "the second reply" in seen["user"]
    # The evolved scene becomes the next base.
    assert state.load_scene("s1") == "the scene, evolved"


def test_shaping_failure_does_not_poison_scene(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(generate, "_gateway_chat", lambda *a, **k: None)
    state.save_scene("s1", "a lighthouse at dusk")
    prompt, used = generate.shape_prompt("raw fallback reply", session="s1")
    assert not used and prompt.startswith("raw fallback reply")
    assert state.load_scene("s1") == "a lighthouse at dusk"


def test_continuity_disabled_always_shapes_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MEDIA_VISUAL_CONTINUITY", "0")
    seen = _capture_chat(monkeypatch)
    state.save_scene("s1", "a lighthouse at dusk")
    generate.shape_prompt("reply", session="s1")
    assert seen["system"] == generate.PROMPT_SHAPER


# --- multi-canvas push --------------------------------------------------------

def _capture_pushes(monkeypatch, fail=()):
    pushed = []

    def fake_push_one(base, payload):
        pushed.append((base, payload))
        return "boom" if base in fail else ""

    monkeypatch.setattr(cli, "_push_one", fake_push_one)
    return pushed


def _image_payload(name):
    def payload_for(targets):
        return {"image": cli._image_ref(name, targets)}
    return payload_for


def test_single_target_uses_bare_name(monkeypatch):
    monkeypatch.setenv("MEDIA_VISUAL_URL", "http://one:8781")
    pushed = _capture_pushes(monkeypatch)
    errs = cli._push_all(_image_payload("img-1.webp"))
    assert errs == [""]
    assert pushed[0][1]["image"] == "img-1.webp"


def test_multi_target_uses_first_targets_absolute_url(monkeypatch):
    monkeypatch.setenv("MEDIA_VISUAL_URL", "http://one:8781, http://two:8781")
    pushed = _capture_pushes(monkeypatch)
    errs = cli._push_all(_image_payload("img-1.webp"))
    assert [b for b, _ in pushed] == ["http://one:8781", "http://two:8781"]
    assert all(p["image"] == "http://one:8781/img/img-1.webp" for _, p in pushed)
    assert errs == ["", ""]


def test_multi_target_partial_failure_reported(monkeypatch):
    monkeypatch.setenv("MEDIA_VISUAL_URL", "http://one:8781 http://two:8781")
    _capture_pushes(monkeypatch, fail={"http://two:8781"})
    errs = cli._push_all(_image_payload("img-1.webp"))
    assert errs == ["", "boom"]


def test_url_list_parsing_defaults_to_localhost(monkeypatch):
    monkeypatch.delenv("MEDIA_VISUAL_URL", raising=False)
    assert cli._canvas_urls() == ["http://127.0.0.1:8781"]
