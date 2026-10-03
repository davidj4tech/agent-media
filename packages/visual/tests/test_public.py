"""The public listener (`--public`, contract §19): app routes only, and the
spool's non-picture files never served as pictures."""

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from agent_media_server import app as _app
from agent_media_visual import canvas


@pytest.fixture
def spool(tmp_path, monkeypatch):
    monkeypatch.setattr(canvas, "spool_dir", lambda: tmp_path)
    (tmp_path / "pair-code").write_text("123456")
    (tmp_path / "last-clip.json").write_text("{}")
    (tmp_path / "img-1-0.svg").write_text("<svg/>")
    return tmp_path


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def public(spool):
    srv, base = _serve(canvas.PublicHandler)
    yield base
    srv.shutdown()


@pytest.fixture
def desk(spool):
    srv, base = _serve(canvas.Handler)
    yield base
    srv.shutdown()


def _status(url, method="GET", headers=None, data=None):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize("path", ["/", "/pageid", "/pair?c=123456", "/peek?pane=x",
                                  "/speech", "/agents", "/events", "/seen", "/sessions",
                                  "/status", "/persona/x/y.png", "/last"])
def test_desk_and_open_reads_are_not_public(public, path):
    assert _status(public + path) == 404


@pytest.mark.parametrize("path", ["/show", "/ctl", "/say", "/play", "/input", "/seen"])
def test_desk_posts_are_not_public(public, path):
    assert _status(public + path, "POST", {"Content-Type": "application/json"}, b"{}") == 404


def test_app_routes_and_health_are_public(public):
    assert _status(public + "/healthz") == 200
    assert _status(public + "/img/img-1-0.svg") == 200
    # Reached and refused by the app's own auth, not hidden.
    assert _status(public + "/targets") == 401


@pytest.mark.parametrize("name", ["pair-code", "last-clip.json"])
def test_spool_files_are_not_pictures(public, desk, name):
    assert _status(public + "/img/" + name) == 404
    assert _status(desk + "/img/" + name) == 404


def test_the_tunnel_names_the_caller(public, monkeypatch):
    seen = []
    real = _app.dispatch

    def spy(h, method, path):
        seen.append(h.client_address[0])
        return real(h, method, path)

    monkeypatch.setattr(_app, "dispatch", spy)
    _status(public + "/targets", headers={"CF-Connecting-IP": "203.0.113.7"})
    _status(public + "/targets", headers={"X-Forwarded-For": "198.51.100.4, 10.0.0.1"})
    _status(public + "/targets")
    assert seen == ["203.0.113.7", "198.51.100.4", "127.0.0.1"]
