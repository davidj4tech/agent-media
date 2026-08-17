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
    assert _post(base, "https://youtu.be/x", path="/nope")[0] == 404


def test_body_parsing():
    assert share_listener._parse_body("https://x/y") == ("https://x/y", "", "")
    assert share_listener._parse_body(
        '{"text":"u","channel":"book","where":"phone"}') == ("u", "book", "phone")
    # A body that opens like JSON but isn't is still shared text, not an error.
    assert share_listener._parse_body("{not json") == ("{not json", "", "")
    assert share_listener._parse_body('{"url":"u"}') == ("u", "", "")


# ---- /recent and /play: what the in-app list is built on ------------------

def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return r.status, json.loads(r.read())


@pytest.fixture(autouse=True)
def _this_host_is_the_origin(monkeypatch):
    """No test may reach for ssh: speech rows are asked of the origin, and
    "unset" means read the config file, which on a real machine names a hub."""
    monkeypatch.setenv("MEDIA_ROLES", "origin render")


@pytest.fixture()
def history(tmp_path, monkeypatch):
    """A state db of our own, so the listing is ours and not this machine's."""
    from agent_media_core.state import StateStore

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return StateStore()


def test_recent_lists_what_played(server, history):
    _, base = server
    history.set_music_intent("mpv:https://www.youtube.com/watch?v=aaaaaaaaaaa",
                             "dj-set", "A Long Set")
    history.set_book_last("https://abs.example/ep12.mp3", "Episode 12")

    code, body = _get(base, "/recent")
    assert code == 200 and body["ok"]
    rows = body["rows"]
    assert [r["channel"] for r in rows] == ["book", "music"]
    assert rows[0]["label"] == "Episode 12"
    assert rows[1]["content_type"] == "dj-set"
    # Every row carries what /play needs, so the app never has to guess.
    assert all(r["uri"] and r["channel"] and r["ago"] for r in rows)
    # And when it happened, not only how long ago: the phone's list groups by
    # day and shows a clock time, and neither can be recovered from "18m".
    now = time.time()
    assert all(now - 60 <= r["started_at"] <= now + 1 for r in rows)


def test_recent_labels_a_row_with_no_title(server, history):
    _, base = server
    history.note_play("music", "mpv:https://www.youtube.com/watch?v=bbbbbbbbbbb")
    rows = _get(base, "/recent")[1]["rows"]
    # The same label `media recent` shows — one implementation, no drift.
    assert rows[0]["label"] == "youtube:bbbbbbbbbbb"


def test_recent_filters_and_limits(server, history):
    _, base = server
    for i in range(5):
        history.note_play("music", f"uri-{i}")
    history.note_play("book", "a-book")
    assert len(_get(base, "/recent?limit=2")[1]["rows"]) == 2
    assert [r["channel"] for r in _get(base, "/recent?channel=book")[1]["rows"]] \
        == ["book"]
    # Junk is clamped rather than fatal: a list must always render something.
    assert _get(base, "/recent?limit=nonsense")[1]["ok"]
    assert _get(base, "/recent?channel=nonsense")[1]["ok"]


def test_recent_speech_rows_come_from_the_clip_list(server, history, monkeypatch):
    # Not from this host's store: a spoken turn is addressed by its history id,
    # and on a render host the local ids belong to different turns entirely.
    _, base = server
    monkeypatch.setattr(
        "agent_media_core.entrypoints.share_control._clips",
        lambda *a, **kw: [{"number": 1, "ref": "5506", "text": "a reply",
                           "at": time.time() - 30, "current": False}])
    rows = _get(base, "/recent?channel=speech")[1]["rows"]
    assert [(r["channel"], r["id"], r["label"]) for r in rows] \
        == [("speech", 5506, "a reply")]
    # No uri: the clips live wherever they were rendered, so the id is the
    # whole of the handle and /control is the door.
    assert rows[0]["uri"] == "" and rows[0]["ago"]


def test_recent_merges_the_channels_in_time_order(server, history, monkeypatch):
    _, base = server
    now = time.time()
    history.note_play("music", "uri-old")
    monkeypatch.setattr(
        "agent_media_core.entrypoints.share_control._clips",
        lambda *a, **kw: [{"number": 1, "ref": "9", "text": "said later",
                           "at": now + 60, "current": False}])
    rows = _get(base, "/recent")[1]["rows"]
    assert [r["channel"] for r in rows] == ["speech", "music"]
    # And the store's own speech rows are not listed twice beside them.
    assert sum(1 for r in rows if r["channel"] == "speech") == 1


def test_recent_on_an_empty_history(server, history):
    _, base = server
    assert _get(base, "/recent")[1] == {"ok": True, "rows": []}


def test_play_repeats_a_row_without_reclassifying(server, dispatched, monkeypatch):
    # The point of the endpoint: no yt-dlp round trip, no chance of landing on
    # a different channel than the row the listener is looking at.
    _, base = server
    monkeypatch.setattr(share, "probe",
                        lambda *a, **kw: pytest.fail("replay must not probe"))

    code, body = _post(base, json.dumps({"uri": "mpv:https://x/y",
                                         "channel": "book",
                                         "content_type": "podcast"}),
                       path="/play")
    assert code == 200 and body["channel"] == "book"
    assert dispatched.wait()
    assert dispatched.calls == [("mpv:https://x/y", "book", "podcast", "")]


def test_play_defaults_to_music(server, dispatched):
    _, base = server
    code, _ = _post(base, json.dumps({"uri": "local:track:x"}), path="/play")
    assert code == 200
    assert dispatched.wait()
    assert dispatched.calls[0][1:3] == ("music", "music")


def test_play_needs_a_uri(server):
    _, base = server
    assert _post(base, json.dumps({"channel": "music"}), path="/play")[0] == 422
    assert _post(base, "not json at all", path="/play")[0] == 422


def test_play_rejects_a_channel_that_does_not_exist(server):
    _, base = server
    code, body = _post(base, json.dumps({"uri": "x", "channel": "speech"}),
                       path="/play")
    assert code == 422 and "speech" in body["error"]


# ---- /channels, /chapters, /control: the app's control screen -------------

def test_channels_answers_all_three(server):
    _, base = server
    code, body = _get(base, "/channels")
    assert code == 200 and body["ok"]
    assert set(body["channels"]) >= {"speech", "music", "book"}


def test_control_presses_one_button(server, monkeypatch):
    _, base = server
    seen = []
    monkeypatch.setattr("agent_media_core.entrypoints.share_control.control",
                        lambda ch, act, arg="": seen.append((ch, act, arg)) or 0)
    code, body = _post(base, json.dumps({"channel": "music", "action": "seek",
                                         "arg": "+30"}), path="/control")
    assert code == 200 and body["ok"] and body["rc"] == 0
    assert seen == [("music", "seek", "+30")]


def test_control_is_synchronous(server, monkeypatch):
    # Unlike a share, a press has no download behind it and the caller is about
    # to re-read /channels — so the answer must already reflect the press.
    _, base = server
    done = []
    monkeypatch.setattr("agent_media_core.entrypoints.share_control.control",
                        lambda *a, **kw: done.append(1) or 0)
    _post(base, json.dumps({"channel": "music", "action": "toggle"}),
          path="/control")
    assert done == [1]


def test_a_refused_verb_is_422(server):
    _, base = server
    code, body = _post(base, json.dumps({"channel": "music", "action": "nope"}),
                       path="/control")
    assert code == 422 and "no such control" in body["error"]


def test_a_failing_command_is_reported_not_hidden(server, monkeypatch):
    _, base = server
    monkeypatch.setattr("agent_media_core.entrypoints.share_control.control",
                        lambda *a, **kw: 1)
    code, body = _post(base, json.dumps({"channel": "book", "action": "next"}),
                       path="/control")
    assert code == 200 and body["ok"] is False and body["rc"] == 1


def test_a_control_that_explodes_does_not_500_bare(server, monkeypatch):
    _, base = server

    def boom(*a, **kw):
        raise RuntimeError("mpv on fire")

    monkeypatch.setattr("agent_media_core.entrypoints.share_control.control", boom)
    code, body = _post(base, json.dumps({"channel": "music", "action": "toggle"}),
                       path="/control")
    assert code == 500 and body["ok"] is False and "on fire" in body["error"]


def test_chapters_endpoint(server, monkeypatch):
    _, base = server
    seen = []
    monkeypatch.setattr("agent_media_core.entrypoints.share_control.chapters",
                        lambda channel="music": seen.append(channel) or [
                            {"number": 1, "title": "Intro",
                             "start_ms": 0, "current": True}])
    code, body = _get(base, "/chapters")
    assert code == 200 and body["rows"][0]["title"] == "Intro"
    # The channel rides in the query, and music is what a caller that does not
    # say gets — which is every caller written before the book had chapters.
    code, _ = _get(base, "/chapters?channel=book")
    assert code == 200 and seen == ["music", "book"]
