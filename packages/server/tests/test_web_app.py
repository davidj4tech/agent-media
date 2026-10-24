"""The Sasonica web app served at /app/ (web_app.py, roadmap item 21): files
as themselves with serve.mjs's caching, client routes as index.html, nothing
outside the bundle, on the canvas's listeners and the canvas-less server, and
`web` in the server report pointing at it. A stand-in bundle in tmp_path."""

from __future__ import annotations

import gzip
import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

from agent_media_server import app, servers_report, web_app

INDEX = b"<!doctype html><title>Sasonica</title><script src=/app/assets/entry-abc.js></script>"
CHUNK = b"console.log('sasonica');\n" * 200


@pytest.fixture()
def bundle(tmp_path, monkeypatch):
    d = tmp_path / "bundle"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_bytes(INDEX)
    (d / "assets" / "entry-abc.js").write_bytes(CHUNK)
    (d / "manifest.webmanifest").write_text('{"start_url": "./"}')
    (tmp_path / "secret.txt").write_text("not yours")
    monkeypatch.setenv("MEDIA_WEB_APP_DIR", str(d))
    return d


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(params=["app", "canvas", "public"])
def server(request):
    if request.param == "app":
        handler = app.Handler
    else:
        from agent_media_visual import canvas
        handler = canvas.Handler if request.param == "canvas" else canvas.PublicHandler
    srv = _serve(handler)
    yield srv.server_address
    srv.shutdown()
    srv.server_close()


def _get(addr, path, headers=None):
    conn = http.client.HTTPConnection(*addr, timeout=5)
    conn.request("GET", path, headers=headers or {})
    res = conn.getresponse()
    body = res.read()
    conn.close()
    return res, body


def test_index_and_client_routes_are_the_app_never_cached(bundle, server):
    for path in ("/app/", "/app/threads", "/app/t/abc-123?x=1", "/app/pairing?server=http://h:8781"):
        res, body = _get(server, path)
        assert res.status == 200, path
        assert body == INDEX, path
        assert res.getheader("Content-Type") == "text/html; charset=utf-8"
        assert res.getheader("Cache-Control") == "no-cache"
        # Static, credential-free: no CORS on it, unlike the API routes.
        assert res.getheader("Access-Control-Allow-Origin") is None


def test_the_bare_prefix_gets_its_slash(bundle, server):
    res, _ = _get(server, "/app?pair=abcd")
    assert res.status == 301 and res.getheader("Location") == "/app/?pair=abcd"


def test_hashed_assets_are_kept_and_gzipped(bundle, server):
    res, body = _get(server, "/app/assets/entry-abc.js")
    assert res.status == 200 and body == CHUNK
    assert res.getheader("Content-Type") == "text/javascript"
    assert "immutable" in res.getheader("Cache-Control")
    res, body = _get(server, "/app/assets/entry-abc.js", {"Accept-Encoding": "gzip, br"})
    assert res.getheader("Content-Encoding") == "gzip" and gzip.decompress(body) == CHUNK
    res, body = _get(server, "/app/manifest.webmanifest")
    assert res.getheader("Content-Type") == "application/manifest+json"
    assert res.getheader("Cache-Control") == "no-cache"


def test_a_missing_asset_is_404_not_the_page(bundle, server):
    res, body = _get(server, "/app/assets/gone-123.js")
    assert res.status == 404 and body != INDEX


def test_nothing_outside_the_bundle(bundle, server):
    for path in ("/app/../secret.txt", "/app/%2e%2e/secret.txt", "/app/assets/../../secret.txt"):
        res, body = _get(server, path)
        assert b"not yours" not in body, path


def test_no_bundle_no_app(tmp_path, monkeypatch, server):
    monkeypatch.setenv("MEDIA_WEB_APP_DIR", str(tmp_path / "empty"))
    res, _ = _get(server, "/app/")
    assert res.status == 404
    assert not web_app.serving()


def test_the_default_is_the_copy_beside_the_module(tmp_path, monkeypatch):
    monkeypatch.delenv("MEDIA_WEB_APP_DIR", raising=False)
    fake = tmp_path / "pkg" / "web_app.py"
    fake.parent.mkdir()
    monkeypatch.setattr(web_app, "__file__", str(fake))
    assert web_app.bundle_dir() is None
    (tmp_path / "pkg" / "web_app").mkdir()
    (tmp_path / "pkg" / "web_app" / "index.html").write_bytes(INDEX)
    assert web_app.bundle_dir() == tmp_path / "pkg" / "web_app"


def test_the_report_names_this_servers_own_app(bundle, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from agent_media_server import tunnel
    monkeypatch.setattr(tunnel, "current_url", lambda: "")
    # No address known yet: nothing to put /app beside.
    assert servers_report.addresses() == {"api": "", "web": ""}
    servers_report.remember_base("http://100.1.2.3:8781/")
    assert servers_report.addresses() == {"api": "http://100.1.2.3:8781", "web": "http://100.1.2.3:8781/app"}
    monkeypatch.setenv("MEDIA_PUBLIC_URL", "https://red5.example")
    assert servers_report.addresses()["web"] == "https://red5.example/app"
    # A web app configured elsewhere wins.
    monkeypatch.setenv("MEDIA_WEB_APP_URL", "https://app.sasonica.com/")
    assert servers_report.addresses()["web"] == "https://app.sasonica.com"


def test_dl_is_its_own_folder_and_a_gone_file_is_404(bundle, server, tmp_path, monkeypatch):
    """APK links live outside the bundle, so a new build keeps them; a missing
    one is a 404, not the app's page (David, 25 Oct 2026)."""
    dl = tmp_path / "dl"
    (dl / "tok").mkdir(parents=True)
    (dl / "tok" / "sasonica-1.apk").write_bytes(b"PK apk")
    monkeypatch.setenv("MEDIA_WEB_DL_DIR", str(dl))
    res, body = _get(server, "/app/dl/tok/sasonica-1.apk")
    assert res.status == 200 and body == b"PK apk"
    assert res.getheader("Content-Type") == "application/vnd.android.package-archive"
    for gone in ("/app/dl/tok/other.apk", "/app/dl/nope/", "/app/dl/../secret.txt", "/app/dl/%2e%2e/bundle/index.html"):
        res, _ = _get(server, gone)
        assert res.status == 404, gone
    # A copy left in the bundle is not what answers.
    (bundle / "dl" / "tok").mkdir(parents=True)
    (bundle / "dl" / "tok" / "old.apk").write_bytes(b"old")
    res, _ = _get(server, "/app/dl/tok/old.apk")
    assert res.status == 404
