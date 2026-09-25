"""A file shared to the app, kept on the host (uploads.py, `POST /upload`)."""

from __future__ import annotations

import datetime as dt
import http.client
import io
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from agent_media_server import app, auth, uploads

DAY = dt.date(2026, 9, 25)


@pytest.fixture()
def shared(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_UPLOAD_DIR", str(tmp_path / "shared"))
    monkeypatch.setattr(auth, "gate", lambda bearer: ({"id": "u"}, {}) if bearer == "good"
                        else (None, {"error": "who?", "status": 401}))
    return tmp_path / "shared"


@pytest.mark.parametrize("given,kept", [
    ("photo.jpg", "photo.jpg"),
    ("../../etc/passwd", "passwd"),
    ("C:\\Users\\x\\scan.pdf", "scan.pdf"),
    (".bashrc", "bashrc"),
    ("a:b*c?.txt", "a-b-c-.txt"),
    ("", "shared"),
    ("x" * 200 + ".jpeg", "x" * 110 + ".jpeg"),
])
def test_safe_name(given, kept):
    assert uploads.safe_name(given) == kept


def test_kept_in_a_folder_per_day_never_over_another(shared):
    ok, got = uploads.save(io.BytesIO(b"one"), 3, "photo.jpg", "good", today=DAY)
    assert ok and got == {"path": str(shared / "2026-09-25" / "photo.jpg"), "name": "photo.jpg", "size": 3}
    ok, got = uploads.save(io.BytesIO(b"two!"), 4, "photo.jpg", "good", today=DAY)
    assert ok and got["name"] == "photo-2.jpg"
    assert (shared / "2026-09-25" / "photo.jpg").read_bytes() == b"one"
    assert (shared / "2026-09-25" / "photo-2.jpg").read_bytes() == b"two!"


def test_refused(shared, monkeypatch):
    assert uploads.save(io.BytesIO(b"x"), 1, "a", "bad")[1]["status"] == 401
    assert uploads.save(io.BytesIO(b""), 0, "a", "good")[1]["status"] == 411
    monkeypatch.setenv("MEDIA_UPLOAD_MAX_MB", "1")
    assert uploads.save(io.BytesIO(b""), (1 << 20) + 1, "a", "good")[1]["status"] == 413
    assert not shared.exists() or not any(shared.rglob("*"))


def test_a_short_body_leaves_nothing(shared):
    ok, got = uploads.save(io.BytesIO(b"abc"), 10, "cut.bin", "good", today=DAY)
    assert not ok and "short" in got["error"]
    assert list((shared / "2026-09-25").iterdir()) == []


@pytest.fixture()
def server(shared):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield srv.server_address
    srv.shutdown()
    srv.server_close()


def _post(addr, path, body, bearer="good"):
    conn = http.client.HTTPConnection(*addr, timeout=5)
    conn.request("POST", path, body=body, headers={"Authorization": f"Bearer {bearer}",
                                                   "Content-Type": "application/octet-stream"})
    res = conn.getresponse()
    out = res.status, json.loads(res.read() or b"{}")
    conn.close()
    return out


def test_the_route_takes_more_than_the_json_cap(server, shared):
    big = b"z" * (app.MAX_BODY * 3)
    status, got = _post(server, "/upload?name=big%20file.bin", big)
    assert status == 200 and got["ok"] and got["size"] == len(big)
    assert got["name"] == "big file.bin"
    assert open(got["path"], "rb").read() == big
    # Every other route keeps its cap.
    conn = http.client.HTTPConnection(*server, timeout=5)
    conn.request("POST", "/reply", body=big, headers={"Content-Type": "application/json"})
    assert conn.getresponse().status == 413
    conn.close()


def test_the_route_refuses_a_stranger(server):
    status, got = _post(server, "/upload?name=a.txt", b"hello", bearer="bad")
    assert status == 401 and got["ok"] is False
