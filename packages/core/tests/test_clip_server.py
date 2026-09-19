"""The clip server answers byte ranges; `python -m http.server` never did."""

from __future__ import annotations

import functools
import http.server
import threading
import urllib.request

import pytest

from agent_media_core.entrypoints import clip_server
from agent_media_core.entrypoints.clip_server import parse_range


@pytest.mark.parametrize("header,want", [
    ("", (None, None)),
    ("bytes=0-9", (0, 9)),
    ("bytes=90-", (90, 99)),
    ("bytes=-10", (90, 99)),
    ("bytes=95-500", (95, 99)),
    ("bytes=100-", (None, -1)),
    ("bytes=0-1,5-6", (None, None)),
    ("items=0-1", (None, None)),
])
def test_parse_range(header, want):
    assert parse_range(header, 100) == want


@pytest.fixture
def server(tmp_path):
    (tmp_path / "m.mka").write_bytes(bytes(range(256)) * 4)
    handler = functools.partial(clip_server.Handler, directory=str(tmp_path))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/m.mka"
    srv.shutdown()


def _get(url, rng=None):
    req = urllib.request.Request(url, headers={"Range": rng} if rng else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), b""


def test_a_range_is_just_those_bytes(server):
    code, hdr, body = _get(server, "bytes=1000-1023")
    assert code == 206
    assert body == bytes(range(1000 % 256, 1000 % 256 + 24))
    assert hdr["Content-Range"] == "bytes 1000-1023/1024"


def test_no_range_is_the_whole_file_and_says_ranges_work(server):
    code, hdr, body = _get(server)
    assert code == 200 and len(body) == 1024
    assert hdr["Accept-Ranges"] == "bytes"


def test_past_the_end_is_416(server):
    code, hdr, _ = _get(server, "bytes=5000-")
    assert code == 416 and hdr["Content-Range"] == "bytes */1024"
