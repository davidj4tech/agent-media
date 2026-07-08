"""The svg engine: animated clip-art from the LLM, validated before serving."""

from agent_media_visual import engines, generate

GOOD_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 900">'
            '<rect width="1600" height="900" fill="#123"/>'
            '<circle cx="800" cy="450" r="90" fill="#fc0">'
            '<animate attributeName="r" values="90;110;90" dur="6s" '
            'repeatCount="indefinite"/></circle></svg>')


def _fake_chat(monkeypatch, reply):
    monkeypatch.setattr(generate, "_gateway_chat", lambda *a, **k: reply)


def test_happy_path(monkeypatch):
    _fake_chat(monkeypatch, GOOD_SVG)
    img, err = generate.generate_svg("a sun over hills")
    assert err == "" and img.startswith(b"<svg")


def test_code_fence_and_preamble_stripped(monkeypatch):
    _fake_chat(monkeypatch, f"Here is your SVG:\n```svg\n{GOOD_SVG}\n```\n")
    img, err = generate.generate_svg("scene")
    assert err == "" and img.startswith(b"<svg") and img.endswith(b"</svg>")


def test_no_svg_element_fails(monkeypatch):
    _fake_chat(monkeypatch, "I cannot draw that.")
    img, err = generate.generate_svg("scene")
    assert img is None and "no <svg>" in err


def test_gateway_failure(monkeypatch):
    _fake_chat(monkeypatch, None)
    img, err = generate.generate_svg("scene")
    assert img is None


def test_script_rejected(monkeypatch):
    _fake_chat(monkeypatch, GOOD_SVG.replace(
        "</svg>", "<script>alert(1)</script></svg>"))
    img, err = generate.generate_svg("scene")
    assert img is None and "script" in err


def test_external_url_rejected(monkeypatch):
    _fake_chat(monkeypatch, GOOD_SVG.replace(
        "</svg>", '<image href="https://evil/x.png"/></svg>'))
    img, err = generate.generate_svg("scene")
    assert img is None


def test_malformed_xml_rejected(monkeypatch):
    _fake_chat(monkeypatch, "<svg><rect></svg>")
    img, err = generate.generate_svg("scene")
    assert img is None and "well-formed" in err


def test_svg_is_a_builtin_engine(monkeypatch):
    monkeypatch.setattr(engines, "entry_points", lambda group: [])
    engines.discover_visual_engines(refresh=True)
    assert "svg" in engines.all_engine_names()
    _fake_chat(monkeypatch, GOOD_SVG)
    img, err = engines.generate_image("a sun", engine="svg")
    assert img.startswith(b"<svg")


def test_bad_svg_falls_back_to_venice(monkeypatch):
    monkeypatch.setattr(engines, "entry_points", lambda group: [])
    engines.discover_visual_engines(refresh=True)
    _fake_chat(monkeypatch, "not markup")
    monkeypatch.setattr(generate, "generate_venice", lambda p: (b"RIFFwebp", ""))
    img, err = engines.generate_image("a sun", engine="svg")
    assert img == b"RIFFwebp"
