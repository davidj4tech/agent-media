"""Dark mode: every engine's palette is biased dark at the prompt level —
the canvas page is black and mostly watched in dim rooms."""

from agent_media_visual import generate


VALID_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 9">'
             '<rect width="16" height="9" fill="#0b0e14"/></svg>')


def test_dark_mode_default_on(monkeypatch):
    monkeypatch.delenv("MEDIA_VISUAL_DARK", raising=False)
    assert generate.dark_mode()


def test_dark_mode_disable(monkeypatch):
    for off in ("0", "off", "no", "false", "OFF"):
        monkeypatch.setenv("MEDIA_VISUAL_DARK", off)
        assert not generate.dark_mode()
    monkeypatch.setenv("MEDIA_VISUAL_DARK", "1")
    assert generate.dark_mode()


def test_svg_system_prompt_gains_dark_clause(monkeypatch):
    monkeypatch.delenv("MEDIA_VISUAL_DARK", raising=False)
    seen = {}

    def fake_chat(system, user, timeout):
        seen["system"] = system
        return VALID_SVG

    monkeypatch.setattr(generate, "_gateway_chat", fake_chat)
    data, err = generate.generate_svg("a lighthouse")
    assert err == "" and data.startswith(b"<svg")
    assert "Dark mode:" in seen["system"]


def test_svg_system_prompt_clean_when_disabled(monkeypatch):
    monkeypatch.setenv("MEDIA_VISUAL_DARK", "0")
    seen = {}

    def fake_chat(system, user, timeout):
        seen["system"] = system
        return VALID_SVG

    monkeypatch.setattr(generate, "_gateway_chat", fake_chat)
    generate.generate_svg("a lighthouse")
    assert "Dark mode:" not in seen["system"]


def test_venice_style_gains_dark_suffix(monkeypatch):
    """The dark suffix rides the style suffix into the venice request body."""
    import json

    monkeypatch.delenv("MEDIA_VISUAL_DARK", raising=False)
    monkeypatch.setattr(generate, "_venice_key", lambda: "k")
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["prompt"] = json.loads(req.data.decode())["prompt"]
        raise OSError("stop here")

    monkeypatch.setattr(generate.urllib.request, "urlopen", fake_urlopen)
    generate.generate_venice("a lighthouse")
    assert generate.DARK_STYLE_SUFFIX in seen["prompt"]

    monkeypatch.setenv("MEDIA_VISUAL_DARK", "0")
    generate.generate_venice("a lighthouse")
    assert generate.DARK_STYLE_SUFFIX not in seen["prompt"]
