"""The page is read when its files change, not once at import.

Every page change used to need a canvas restart on every host that runs one.
Now a request stats the page's files and re-reads them only when one moved, and
an open stream is told of the new page with another hello.
"""

import http.client
import json
import os
import shutil
import socket
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from agent_media_visual import canvas


@pytest.fixture()
def static(tmp_path, monkeypatch):
    d = tmp_path / "static"
    shutil.copytree(canvas._STATIC, d)
    monkeypatch.setattr(canvas, "_STATIC", d)
    # Fresh caches over the copy, so the real module state is untouched.
    monkeypatch.setattr(canvas, "_PAGE", canvas._Assembled(
        canvas._PAGE.names, canvas._PAGE.build))
    monkeypatch.setattr(canvas, "_VIEW", canvas._Assembled(
        canvas._VIEW.names, canvas._VIEW.build))
    monkeypatch.setattr(canvas, "_PAGE_CHECK_S", 0.05)
    for k in ("PAGE", "PAGE_ID", "VIEW_PAGE"):
        monkeypatch.setattr(canvas, k, getattr(canvas, k))
    return d


def _settled(path, text):
    """Write a file as a finished deploy would leave it: older than the settle."""
    path.write_text(text, encoding="utf-8")
    old = time.time() - 10
    os.utime(path, (old, old))


@pytest.fixture()
def server(static):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), canvas.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address
    srv.shutdown()
    srv.server_close()


def _get(addr, path):
    conn = http.client.HTTPConnection(*addr, timeout=5)
    conn.request("GET", path)
    res = conn.getresponse()
    return res.read().decode()


def test_a_changed_page_file_is_served_without_a_restart(server, static):
    before = _get(server, "/")
    old_id = _get(server, "/pageid").strip()
    js = (static / "canvas.js").read_text(encoding="utf-8")
    _settled(static / "canvas.js", js + "\n// a new page\n")
    after = _get(server, "/")
    assert "// a new page" not in before and "// a new page" in after
    assert _get(server, "/pageid").strip() != old_id


def test_the_viewer_page_too(static):
    _settled(static / "view.html", "<!doctype html><p>new viewer")
    assert canvas.view_page() == "<!doctype html><p>new viewer"


def test_unchanged_files_are_not_read_again(static, monkeypatch):
    canvas.page()
    reads = []
    real = canvas._PAGE.build
    monkeypatch.setattr(canvas._PAGE, "build",
                        lambda read: reads.append(1) or real(read))
    for _ in range(5):
        canvas.page()
    assert reads == []


def test_same_bytes_same_id(static):
    first = canvas.page_id()
    p = static / "canvas.css"
    _settled(p, p.read_text(encoding="utf-8"))      # touched, not changed
    assert canvas.page_id() == first


def test_a_file_still_being_written_waits(static):
    canvas.page()
    first = canvas.page_id()
    (static / "canvas.js").write_text("half a fi", encoding="utf-8")  # mtime: now
    assert canvas.page_id() == first
    old = time.time() - 10
    os.utime(static / "canvas.js", (old, old))
    assert canvas.page_id() != first


def test_an_open_stream_is_told_of_the_new_page_once(server, static, monkeypatch):
    monkeypatch.setattr(canvas.HUB, "last", None)
    monkeypatch.setattr(canvas.HUB, "last_state", None)
    monkeypatch.setattr(canvas.HUB, "last_video", None)
    s = socket.create_connection(server, timeout=5)
    s.sendall(b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n")

    def hellos(buf):
        return [json.loads(f.removeprefix(b"data: ")) for f in buf.split(b"\n\n")
                if f.startswith(b"data: ") and b'"hello"' in f]

    buf = b""
    while not hellos(buf):
        buf += s.recv(4096)
    first = hellos(buf)[0]["page"]
    _settled(static / "canvas.html",
             (static / "canvas.html").read_text(encoding="utf-8") + "<!-- new -->")
    deadline = time.monotonic() + 3
    s.settimeout(0.2)
    while time.monotonic() < deadline:
        try:
            buf += s.recv(4096)
        except TimeoutError:
            pass
    s.close()
    ids = [h["page"] for h in hellos(buf)]
    assert ids == [first, canvas._PAGE.id] and first != ids[1]
