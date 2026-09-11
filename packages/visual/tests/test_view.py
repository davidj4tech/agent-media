"""One address, two answers: the picture, and the page that shows it.

Sasonica draws the picture under a message as an ``<img>`` and opens the very
same URL in the browser when it is tapped. So ``/img/<name>`` has to return
bytes to the chat and a viewer page to the tap, with neither end knowing which
it is asking for. Everything ambiguous falls to the bytes: the viewer is an
improvement on a tap, never a condition of the picture loading.

The header strings below are **verbatim Chrome**, captured off the wire rather
than written from memory, and that is the point of this file. The first version
of this route read ``Sec-Fetch-Dest``, which is the header that means exactly
this — and which browsers send only to potentially-trustworthy origins. The
canvas is plain http on a tailnet host, so the real phone sent none of it and
every tap got a bare image. The tests all passed: they set the header by hand,
and the browser harness drives 127.0.0.1, which is trustworthy. So there is a
case here for a navigation that carries no ``Sec-Fetch-*`` at all — that case
is the deployment.
"""

import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

from agent_media_visual import canvas


@pytest.fixture()
def spool(tmp_path, monkeypatch):
    monkeypatch.setattr(canvas, "spool_dir", lambda: tmp_path)
    (tmp_path / "fig.png").write_bytes(b"\x89PNG\r\n\x1a\nnot really")
    (tmp_path / "fig.svg").write_text("<svg/>")
    return tmp_path


@pytest.fixture()
def server(spool):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), canvas.Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
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


# Verbatim Chrome, over plain http to a tailnet host: no Sec-Fetch-* at all.
NAV = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                 "image/avif,image/webp,image/apng,*/*;q=0.8,"
                 "application/signed-exchange;v=b3;q=0.7"}
TAG = {"Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,"
                 "*/*;q=0.8"}
# The same two over https or localhost, where the browser adds the metadata.
IMG = {**TAG, "Sec-Fetch-Dest": "image"}
DOC = {**NAV, "Sec-Fetch-Dest": "document"}


def test_an_img_tag_still_gets_the_picture(server):
    res, body = _get(server, "/img/fig.png", IMG)
    assert res.status == 200
    assert res.getheader("Content-Type") == "image/png"
    assert body.startswith(b"\x89PNG")


def test_a_tap_gets_the_viewer(server):
    res, body = _get(server, "/img/fig.png", DOC)
    assert res.status == 200
    assert res.getheader("Content-Type").startswith("text/html")
    assert b'id="pic"' in body and b'id="full"' in body


def test_the_viewer_asks_for_its_own_picture_raw(server):
    # ?raw=1 is how the page declines the viewer it was just served, so it must
    # come back as bytes even on a navigation-shaped request.
    _, page = _get(server, "/img/fig.png", DOC)
    assert b"'?raw=1'" in page
    res, body = _get(server, "/img/fig.png?raw=1", DOC)
    assert res.getheader("Content-Type") == "image/png"
    assert body.startswith(b"\x89PNG")


def test_view_1_asks_for_the_viewer_without_a_header(server):
    # So a link can be deliberate rather than relying on the browser's word.
    res, body = _get(server, "/img/fig.png?view=1")
    assert res.getheader("Content-Type").startswith("text/html")
    assert b'id="pic"' in body


def test_an_unmarked_client_gets_the_picture(server):
    # curl, the app's native HTTP plugin, anything that names no type it wants:
    # the answer is the behaviour this route has always had. `*/*` is not a
    # request for a page, and neither is an <img>'s `*/*;q=0.8` tail.
    for headers in ({}, {"Sec-Fetch-Dest": ""}, {"Accept": "*/*"},
                    {"Accept": ""}, TAG):
        res, body = _get(server, "/img/fig.png", headers)
        assert res.getheader("Content-Type") == "image/png", headers
        assert body.startswith(b"\x89PNG")


def test_a_plain_http_navigation_gets_the_viewer(server):
    # THE regression. Chrome attaches Sec-Fetch-* only to potentially
    # trustworthy origins, and the canvas is plain http on a tailnet host — so
    # a real tap on David's phone carries none of it. Accept is what survives.
    assert "Sec-Fetch-Dest" not in NAV
    res, body = _get(server, "/img/fig.png", NAV)
    assert res.getheader("Content-Type").startswith("text/html")
    assert b'id="full"' in body


def test_an_img_tag_on_plain_http_still_gets_bytes(server):
    # The other half of the same fix: the chat's thumbnails must not turn into
    # HTML pages because the header they were being told apart by went away.
    res, body = _get(server, "/img/fig.png", TAG)
    assert res.getheader("Content-Type") == "image/png"
    assert body.startswith(b"\x89PNG")


def test_both_answers_say_what_they_turn_on(server):
    # One address, two bodies, and a day of immutable caching on one of them:
    # without Vary a cache that saw the <img> first may hand those bytes to the
    # tap, and the viewer goes missing for as long as the entry lives.
    res, _ = _get(server, "/img/fig.png", TAG)
    assert "Accept" in res.getheader("Vary")
    res, _ = _get(server, "/img/fig.png", NAV)
    assert "Accept" in res.getheader("Vary")
    assert res.getheader("Cache-Control") == "no-store"


def test_a_missing_picture_is_404_before_any_viewer(server):
    # A viewer page for a picture that does not exist is a blank screen with a
    # fullscreen button on it — say 404 instead, whoever is asking.
    for headers in (DOC, IMG):
        res, _ = _get(server, "/img/nope.png", headers)
        assert res.status == 404, headers


def test_the_viewer_carries_the_landscape_lock(server):
    _, page = _get(server, "/img/fig.png", DOC)
    assert b"requestFullscreen" in page
    assert b"orientation.lock('landscape')" in page
    # ...and withholds it on e-ink, where nothing else on these pages moves.
    assert b"eink()" in page


def test_svg_is_marked_inkable_by_its_extension(server):
    # The viewer decides from its own address (there is no state to consult),
    # so the rule has to survive being read off location.pathname.
    _, page = _get(server, "/img/fig.svg", DOC)
    assert b"inkable" in page
