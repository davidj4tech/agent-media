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


@pytest.fixture()
def dispatched(monkeypatch):
    """Capture what the background thread would have played, and let a test
    wait for it.

    Patches `_play` — the thread's target — rather than `share.dispatch`
    underneath it. The distinction is not cosmetic: the target is bound when
    the handler spawns the thread, but anything it looks up *inside* itself
    resolves when the thread finally runs, which can be after the test has
    returned and monkeypatch has put the real function back. That race made
    this suite occasionally call the real `music play` on the developer's
    machine — mpv, Mopidy, the lot — and the visible symptom was an unrelated
    test failing on breaker state written by the leak.

    `wait()` also gives every test a way to leave nothing in flight.
    """
    calls = []
    ran = threading.Event()
    gate = threading.Event()
    gate.set()  # tests that want to hold the thread open clear it

    def fake_play(url, verdict, where):
        gate.wait(10)
        calls.append((url, verdict.channel, verdict.content_type, where))
        ran.set()

    monkeypatch.setattr(share_listener, "_play", fake_play)
    fake_play.calls = calls
    fake_play.gate = gate
    fake_play.wait = lambda timeout=5: ran.wait(timeout)
    yield fake_play
    gate.set()
    ran.wait(10)  # never leave a dispatch thread running into the next test


def test_a_share_is_classified_and_dispatched(server, monkeypatch, dispatched):
    _, base = server
    monkeypatch.setattr(share, "probe",
                        lambda url, **kw: share.Probe(url=url, probed=True,
                                                      title="A Talk",
                                                      duration_s=5400,
                                                      categories=["Education"]))
    code, body = _post(base, "A Talk https://youtu.be/jNQXAC9IVRw")
    assert code == 200
    assert body["channel"] == "book" and body["content_type"] == "podcast"
    assert body["url"] == "https://youtu.be/jNQXAC9IVRw"
    assert body["title"] == "A Talk"
    assert dispatched.wait()
    assert dispatched.calls == [
        ("https://youtu.be/jNQXAC9IVRw", "book", "podcast", "")]


def test_the_response_does_not_wait_for_playback(server, monkeypatch, dispatched):
    # The property the whole split exists for: acquisition can take minutes on
    # a phone, and the toast must not wait for it.
    _, base = server
    monkeypatch.setattr(share, "probe",
                        lambda url, **kw: share.Probe(url=url, probed=True))
    dispatched.gate.clear()  # the "download" hangs

    started = time.monotonic()
    code, _ = _post(base, "https://youtu.be/jNQXAC9IVRw")
    elapsed = time.monotonic() - started
    assert code == 200
    assert elapsed < 2.0, f"the response waited {elapsed:.1f}s for playback"
    dispatched.gate.set()
    assert dispatched.wait()


def test_a_json_body_carries_the_channel_override(server, monkeypatch, dispatched):
    _, base = server
    monkeypatch.setattr(share, "probe",
                        lambda url, **kw: share.Probe(url=url, probed=True))
    code, body = _post(base, json.dumps({"text": "https://youtu.be/x",
                                         "channel": "book",
                                         "where": "phone"}))
    assert code == 200 and body["channel"] == "book"
    assert dispatched.wait()
    assert dispatched.calls == [("https://youtu.be/x", "book", "music", "phone")]


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
