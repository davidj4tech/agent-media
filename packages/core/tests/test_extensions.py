"""Tests for the render-engine extension contract (extensions.py + render_text
third-party dispatch)."""

from pathlib import Path

import pytest

from agent_media_core import extensions
from agent_media_core.render import render_text


class _FakeEP:
    """Minimal stand-in for importlib.metadata.EntryPoint."""

    def __init__(self, name, fn):
        self.name = name
        self.value = f"<fake:{name}>"
        self._fn = fn

    def load(self):
        if isinstance(self._fn, Exception):
            raise self._fn
        return self._fn


@pytest.fixture(autouse=True)
def _clear_cache():
    extensions._cache = None
    yield
    extensions._cache = None


def _patch_eps(monkeypatch, eps):
    monkeypatch.setattr(extensions, "entry_points", lambda group: list(eps))


def _good_engine(text, outfile, *, voice=None):
    Path(outfile).write_bytes(b"RIFF....fake-wav")
    return True, ""


def test_discovers_third_party_engine(monkeypatch):
    _patch_eps(monkeypatch, [_FakeEP("espeak", _good_engine)])
    engines = extensions.discover_render_engines(refresh=True)
    assert set(engines) == {"espeak"}
    names = extensions.all_engine_names()
    assert "espeak" in names
    # built-ins still present and listed first
    n = len(extensions.BUILTIN_ENGINE_NAMES)
    assert names[:n] == extensions.BUILTIN_ENGINE_NAMES
    assert "edge" in names


def test_extension_cannot_shadow_builtin(monkeypatch):
    _patch_eps(monkeypatch, [_FakeEP("edge", _good_engine)])
    engines = extensions.discover_render_engines(refresh=True)
    assert "edge" not in engines  # built-in wins; shadow ignored


def test_broken_engine_is_skipped_not_fatal(monkeypatch):
    _patch_eps(monkeypatch, [
        _FakeEP("boom", ImportError("no module")),
        _FakeEP("notcallable", 123),
        _FakeEP("ok", _good_engine),
    ])
    engines = extensions.discover_render_engines(refresh=True)
    assert set(engines) == {"ok"}  # the two bad ones are dropped, not raised


def test_render_text_dispatches_to_extension(monkeypatch, tmp_path):
    _patch_eps(monkeypatch, [_FakeEP("espeak", _good_engine)])
    extensions.discover_render_engines(refresh=True)  # prime cache
    out = tmp_path / "clip.wav"
    ok, err = render_text("hello", out, engine="espeak", fallback_to_edge=False)
    assert ok and err == ""
    assert out.read_bytes().startswith(b"RIFF")


def test_unknown_engine_still_errors(monkeypatch, tmp_path):
    _patch_eps(monkeypatch, [])
    extensions.discover_render_engines(refresh=True)
    ok, err = render_text("hi", tmp_path / "x.wav", engine="nope",
                          fallback_to_edge=False)
    assert not ok and "unknown engine" in err


def test_extension_exception_is_isolated(monkeypatch, tmp_path):
    def _raises(text, outfile, *, voice=None):
        raise RuntimeError("kaboom")

    _patch_eps(monkeypatch, [_FakeEP("bad", _raises)])
    extensions.discover_render_engines(refresh=True)
    ok, err = render_text("hi", tmp_path / "x.wav", engine="bad",
                          fallback_to_edge=False)
    assert not ok and "kaboom" in err
