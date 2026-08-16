"""The loopback endpoint the share sheet posts to.

Two properties matter more than the routing: it **answers before it plays**
(the sharer must not watch a spinner through a download), and it never returns
a traceback — an activity showing a stack trace in a toast is worse than one
showing "no link in the shared text".
"""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from agent_media_core import share
from agent_media_core.entrypoints import share_listener


@pytest.fixture()
def server(monkeypatch):
    """A listener on an ephemeral port, with the probe and the dispatch faked.

    The real ones shell out to yt-dlp and start playback; neither belongs in a
    unit test, and both are covered on their own in test_share.py.
    """
    srv = share_listener._Server(("127.0.0.1", 0), share_listener.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _post(base, body, path="/share"):
    req = urllib.request.Request(base + path, data=body.encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_get_is_a_health_probe(server):
    _, base = server
    with urllib.request.urlopen(base + "/", timeout=5) as r:
        assert json.loads(r.read())["ok"] is True


def test_a_share_is_classified_and_dispatched(server, monkeypatch):
    _, base = server
    monkeypatch.setattr(share, "probe",
                        lambda url, **kw: share.Probe(url=url, probed=True,
                                                      title="A Talk",
                                                      duration_s=5400,
                                                      categories=["Education"]))
    played = []
    done = threading.Event()

    def fake_dispatch(url, verdict, where=""):
        played.append((url, verdict.channel, where))
        done.set()
        return 0

    monkeypatch.setattr(share, "dispatch", fake_dispatch)

    code, body = _post(base, "A Talk https://youtu.be/jNQXAC9IVRw")
    assert code == 200
    assert body["channel"] == "book" and body["content_type"] == "podcast"
    assert body["url"] == "https://youtu.be/jNQXAC9IVRw"
    assert body["title"] == "A Talk"
    assert done.wait(5)
    assert played == [("https://youtu.be/jNQXAC9IVRw", "book", "")]


def test_the_response_does_not_wait_for_playback(server, monkeypatch):
    # The property the whole split exists for: acquisition can take minutes on
    # a phone, and the toast must not wait for it.
    _, base = server
    monkeypatch.setattr(share, "probe",
                        lambda url, **kw: share.Probe(url=url, probed=True))
    release = threading.Event()
    monkeypatch.setattr(share, "dispatch",
                        lambda *a, **kw: (release.wait(10), 0)[1])

    started = time.monotonic()
    code, _ = _post(base, "https://youtu.be/jNQXAC9IVRw")
    elapsed = time.monotonic() - started
    release.set()
    assert code == 200
    assert elapsed < 2.0, f"the response waited {elapsed:.1f}s for playback"


def test_a_json_body_carries_the_channel_override(server, monkeypatch):
    _, base = server
    monkeypatch.setattr(share, "probe",
                        lambda url, **kw: share.Probe(url=url, probed=True))
    seen = []
    done = threading.Event()
    monkeypatch.setattr(share, "dispatch",
                        lambda url, v, where="": (
                            seen.append((v.channel, where)), done.set(), 0)[2])

    code, body = _post(base, json.dumps({"text": "https://youtu.be/x",
                                         "channel": "book",
                                         "where": "phone"}))
    assert code == 200 and body["channel"] == "book"
    assert done.wait(5) and seen == [("book", "phone")]


def test_text_with_no_link_is_a_422_not_a_500(server):
    _, base = server
    code, body = _post(base, "just some words")
    assert code == 422
    assert body["ok"] is False and "no link" in body["error"]


def test_a_classifier_explosion_is_reported_not_leaked(server, monkeypatch):
    _, base = server

    def boom(url, **kw):
        raise RuntimeError("probe on fire")

    monkeypatch.setattr(share, "probe", boom)
    code, body = _post(base, "https://youtu.be/x")
    assert code == 500 and body["ok"] is False


def test_an_empty_body_is_rejected(server):
    _, base = server
    assert _post(base, "")[0] == 400


def test_an_oversized_body_is_rejected(server):
    _, base = server
    assert _post(base, "x" * (share_listener.MAX_BODY + 1))[0] == 400


def test_unknown_paths_404(server):
    _, base = server
    assert _post(base, "https://youtu.be/x", path="/play")[0] == 404


def test_body_parsing():
    assert share_listener._parse_body("https://x/y") == ("https://x/y", "", "")
    assert share_listener._parse_body(
        '{"text":"u","channel":"book","where":"phone"}') == ("u", "book", "phone")
    # A body that opens like JSON but isn't is still shared text, not an error.
    assert share_listener._parse_body("{not json") == ("{not json", "", "")
    assert share_listener._parse_body('{"url":"u"}') == ("u", "", "")
