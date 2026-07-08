"""The visual_engines registry: discovery, shadow rule, dispatch, fallback."""

from agent_media_visual import engines


class _EP:
    def __init__(self, name, fn=None, raises=False):
        self.name = name
        self.value = f"fake:{name}"
        self._fn = fn
        self._raises = raises

    def load(self):
        if self._raises:
            raise ImportError("broken plugin")
        return self._fn


def _install(monkeypatch, eps):
    monkeypatch.setattr(engines, "entry_points", lambda group: eps)
    engines.discover_visual_engines(refresh=True)


def test_discovery_skips_shadow_broken_and_duplicates(monkeypatch):
    good = lambda p: (b"img", "")  # noqa: E731
    _install(monkeypatch, [
        _EP("venice", good),          # shadows the built-in → ignored
        _EP("broken", raises=True),   # import error → skipped
        _EP("mine", good),
        _EP("mine", lambda p: (b"other", "")),  # duplicate → first wins
        _EP("notcallable", fn="a string"),
    ])
    found = engines.discover_visual_engines(refresh=True)
    assert set(found) == {"mine"}
    assert engines.all_engine_names() == ("venice", "svg", "mine")


def test_dispatch_to_plugin(monkeypatch):
    _install(monkeypatch, [_EP("mine", lambda p: (b"plugin-bytes", ""))])
    img, err = engines.generate_image("a scene", engine="mine")
    assert img == b"plugin-bytes" and err == ""


def test_unknown_engine_falls_back_to_venice(monkeypatch):
    _install(monkeypatch, [])
    calls = {}

    def fake_venice(prompt):
        calls["prompt"] = prompt
        return b"venice-bytes", ""

    from agent_media_visual import generate as g
    monkeypatch.setattr(g, "generate_venice", fake_venice)
    img, err = engines.generate_image("a scene", engine="nope")
    assert img == b"venice-bytes" and calls["prompt"] == "a scene"


def test_plugin_failure_falls_back(monkeypatch):
    _install(monkeypatch, [_EP("mine", lambda p: (None, "quota"))])
    from agent_media_visual import generate as g
    monkeypatch.setattr(g, "generate_venice", lambda p: (b"vb", ""))
    img, err = engines.generate_image("x", engine="mine")
    assert img == b"vb"


def test_plugin_raise_is_isolated(monkeypatch):
    def boom(prompt):
        raise RuntimeError("kaput")

    _install(monkeypatch, [_EP("mine", boom)])
    from agent_media_visual import generate as g
    monkeypatch.setattr(g, "generate_venice", lambda p: (b"vb", ""))
    img, err = engines.generate_image("x", engine="mine")
    assert img == b"vb"


def test_both_engines_failing_reports_chain(monkeypatch):
    _install(monkeypatch, [_EP("mine", lambda p: (None, "quota"))])
    from agent_media_visual import generate as g
    monkeypatch.setattr(g, "generate_venice", lambda p: (None, "no key"))
    img, err = engines.generate_image("x", engine="mine")
    assert img is None
    assert "quota" in err and "venice" in err and "no key" in err


def test_venice_failing_does_not_fall_back_to_itself(monkeypatch):
    _install(monkeypatch, [])
    from agent_media_visual import generate as g
    calls = []
    monkeypatch.setattr(g, "generate_venice",
                        lambda p: (calls.append(1), (None, "down"))[1])
    img, err = engines.generate_image("x")   # default engine = venice
    assert img is None and err == "down" and len(calls) == 1


def test_env_selects_engine(monkeypatch):
    _install(monkeypatch, [_EP("mine", lambda p: (b"mine-bytes", ""))])
    monkeypatch.setenv("MEDIA_VISUAL_ENGINE", "mine")
    img, err = engines.generate_image("x")
    assert img == b"mine-bytes"
