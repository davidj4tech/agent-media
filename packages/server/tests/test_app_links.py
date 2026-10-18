"""`/.well-known/assetlinks.json` (app_links.py): the Sasonica app's packages
and keys, open on every listener (the public one is the one Android asks), so
a thread link `https://<server>/app/t/<session>` opens in the app."""

from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from agent_media_server import app, app_links


@pytest.fixture(params=["app", "canvas", "public"])
def server(request):
    if request.param == "app":
        handler = app.Handler
    else:
        from agent_media_visual import canvas
        handler = canvas.Handler if request.param == "canvas" else canvas.PublicHandler
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address
    srv.shutdown()
    srv.server_close()


def _get(addr, path):
    conn = http.client.HTTPConnection(*addr, timeout=5)
    conn.request("GET", path)
    res = conn.getresponse()
    body = res.read()
    conn.close()
    return res, body


def test_names_the_app_and_its_keys_with_no_credential(server, monkeypatch):
    monkeypatch.delenv("MEDIA_APP_LINKS_EXTRA", raising=False)
    res, body = _get(server, "/.well-known/assetlinks.json")
    assert res.status == 200
    assert res.getheader("Content-Type") == "application/json"
    got = {s["target"]["package_name"]: s["target"]["sha256_cert_fingerprints"] for s in json.loads(body)}
    assert got == app_links.APPS
    for s in json.loads(body):
        assert s["relation"] == ["delegate_permission/common.handle_all_urls"]
        assert s["target"]["namespace"] == "android_app"


def test_an_extra_key_is_added(monkeypatch):
    monkeypatch.setenv("MEDIA_APP_LINKS_EXTRA", "com.sasonica.app=aa:bb com.example.fork=cc:dd")
    got = {s["target"]["package_name"]: s["target"]["sha256_cert_fingerprints"] for s in app_links.statements()}
    assert got["com.sasonica.app"] == app_links.APPS["com.sasonica.app"] + ["AA:BB"]
    assert got["com.example.fork"] == ["CC:DD"]
    assert got["com.sasonica.app.debug"] == app_links.APPS["com.sasonica.app.debug"]
