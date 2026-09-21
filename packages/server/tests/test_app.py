"""The server on its own: no canvas imported, and a Handler that serves only
the app's routes.

test_contract.py drives the canvas's handler, which is what clients reach
today; this is the other half of the split — that nothing here needs it.
"""

from __future__ import annotations

import http.client
import os
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

from agent_media_server import app


def test_the_server_never_imports_the_canvas():
    code = ("import sys, agent_media_server.app\n"
            "print(sorted(m for m in sys.modules if m.startswith('agent_media_visual')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=os.environ.copy(), timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True)
    t.start()
    yield srv.server_address
    srv.shutdown()
    srv.server_close()


def _request(addr, method, path):
    conn = http.client.HTTPConnection(*addr, timeout=5)
    conn.request(method, path)
    res = conn.getresponse()
    res.read()
    conn.close()
    return res


def test_an_app_route_answers_without_a_canvas(server):
    # No bearer: refused before Audiobookshelf is asked anything.
    res = _request(server, "GET", "/targets")
    assert res.status == 401
    assert res.getheader("Access-Control-Allow-Origin") == "*"


def test_the_canvas_routes_are_not_here(server):
    assert _request(server, "GET", "/").status == 404
    assert _request(server, "GET", "/events").status == 404


def test_preflight_is_the_app_routes_only(server):
    assert _request(server, "OPTIONS", "/reply").status == 204
    assert _request(server, "OPTIONS", "/input").status == 405
