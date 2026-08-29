"""The door a podcast client knocks on.

Two properties carry most of these: a capability URL has to reach the
enclosures as well as the feed (or the client lists episodes it cannot
download), and byte ranges have to work (or a resumed download starts again
from zero).
"""

import threading
import urllib.error
import urllib.request

import pytest

from agent_media_core import feed
from agent_media_core.entrypoints import feed_server


@pytest.fixture
def spool(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_FEED_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "nope.toml"))
    monkeypatch.delenv("MEDIA_FEED_BASE_URL", raising=False)
    return tmp_path


BODY = b"ID3" + bytes(range(256)) * 8          # 2051 bytes, distinctive


@pytest.fixture
def published(spool):
    src = spool / "clip.mp3"
    src.write_bytes(BODY)
    return feed.publish("talks", src, guid="s-1", title="One",
                        description="about ranges", duration_s=12.0,
                        published=1_700_000_000.0)


@pytest.fixture
def server(spool, monkeypatch, request):
    token = getattr(request, "param", "")
    monkeypatch.setattr(feed_server, "TOKEN", token)
    srv = feed_server.serve("127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", token
    srv.shutdown()
    srv.server_close()
    t.join(timeout=5)


def _get(url, headers=None, method="GET"):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    return urllib.request.urlopen(req, timeout=10)


def _status(url, headers=None):
    try:
        return _get(url, headers).status
    except urllib.error.HTTPError as e:
        return e.code


# --- auth ------------------------------------------------------------------

@pytest.mark.parametrize("server", ["s3cret"], indirect=True)
def test_without_the_token_nothing_is_readable(server, published):
    base, _ = server
    assert _status(f"{base}/feed/talks.xml") == 401
    assert _status(f"{base}/ep/talks/{published.filename}") == 401
    assert _status(f"{base}/") == 401


@pytest.mark.parametrize("server", ["s3cret"], indirect=True)
def test_the_token_may_arrive_three_ways(server, published):
    import base64
    base, tok = server
    assert _status(f"{base}/feed/talks.xml?k={tok}") == 200
    assert _status(f"{base}/feed/talks.xml",
                   {"X-Agent-Media-Token": tok}) == 200
    basic = base64.b64encode(f"anyone:{tok}".encode()).decode()
    assert _status(f"{base}/feed/talks.xml", {"Authorization": f"Basic {basic}"}) == 200
    assert _status(f"{base}/feed/talks.xml?k=wrong") == 401


@pytest.mark.parametrize("server", ["s3cret"], indirect=True)
def test_a_url_token_reaches_the_enclosures(server, published):
    """Otherwise the feed loads and every episode 401s — which reads to the
    user as a broken server rather than a wrong token."""
    base, tok = server
    xml = _get(f"{base}/feed/talks.xml?k={tok}").read().decode()
    assert f"/ep/talks/{published.filename}?k={tok}" in xml


@pytest.mark.parametrize("server", ["s3cret"], indirect=True)
def test_a_header_token_is_not_baked_into_urls(server, published):
    """A client that got in with a header will send it again; putting the
    secret in URLs it never needed is how a token ends up in a sync log."""
    base, tok = server
    xml = _get(f"{base}/feed/talks.xml",
               {"X-Agent-Media-Token": tok}).read().decode()
    assert "?k=" not in xml
    assert f"/ep/talks/{published.filename}" in xml


def test_no_token_configured_means_open(server, published):
    base, _ = server
    assert _status(f"{base}/feed/talks.xml") == 200


# --- the feed --------------------------------------------------------------

def test_the_host_the_client_used_is_the_host_in_the_enclosures(server, published):
    base, _ = server
    host = base.removeprefix("http://")
    xml = _get(f"{base}/feed/talks.xml").read().decode()
    assert f"http://{host}/ep/talks/" in xml


def test_base_url_override_wins(server, published, monkeypatch):
    monkeypatch.setenv("MEDIA_FEED_BASE_URL", "https://feeds.example/")
    base, _ = server
    xml = _get(f"{base}/feed/talks.xml").read().decode()
    assert "https://feeds.example/ep/talks/" in xml


def test_the_feed_is_generated_not_read_from_disk(server, spool, published):
    """A stale feed.xml on disk must not be what a client gets."""
    (feed.feed_dir("talks") / "feed.xml").write_text("<rss>stale</rss>")
    xml = _get(f"{server[0]}/feed/talks.xml").read().decode()
    assert "stale" not in xml and "One" in xml


def test_an_unchanged_feed_answers_304(server, published):
    base, _ = server
    r = _get(f"{base}/feed/talks.xml")
    etag = r.headers["ETag"]
    assert _status(f"{base}/feed/talks.xml", {"If-None-Match": etag}) == 304


def test_publishing_changes_the_etag(server, spool, published):
    base, _ = server
    etag = _get(f"{base}/feed/talks.xml").headers["ETag"]
    src = spool / "two.mp3"
    src.write_bytes(BODY)
    feed.publish("talks", src, guid="s-2", title="Two", duration_s=1.0)
    assert _get(f"{base}/feed/talks.xml").headers["ETag"] != etag


def test_unknown_feed_is_404(server, published):
    assert _status(f"{server[0]}/feed/nothing.xml") == 404


def test_the_index_lists_subscribable_urls(server, published):
    base, _ = server
    body = _get(f"{base}/").read().decode()
    assert f"{base}/feed/talks.xml" in body and "1 episode" in body


# --- episodes --------------------------------------------------------------

def test_a_whole_episode_comes_back_whole(server, published):
    r = _get(f"{server[0]}/ep/talks/{published.filename}")
    assert r.status == 200
    assert r.headers["Content-Type"] == "audio/mpeg"
    assert r.headers["Accept-Ranges"] == "bytes"
    assert r.read() == BODY


def test_head_gives_the_size_without_the_body(server, published):
    r = _get(f"{server[0]}/ep/talks/{published.filename}", method="HEAD")
    assert int(r.headers["Content-Length"]) == len(BODY)
    assert r.read() == b""


def test_a_range_resumes_where_a_download_stopped(server, published):
    r = _get(f"{server[0]}/ep/talks/{published.filename}",
             {"Range": "bytes=1000-"})
    assert r.status == 206
    assert r.headers["Content-Range"] == f"bytes 1000-{len(BODY)-1}/{len(BODY)}"
    assert r.read() == BODY[1000:]


def test_a_closed_range_returns_exactly_that_slice(server, published):
    r = _get(f"{server[0]}/ep/talks/{published.filename}",
             {"Range": "bytes=10-19"})
    assert r.status == 206 and r.read() == BODY[10:20]


def test_a_suffix_range_returns_the_tail(server, published):
    r = _get(f"{server[0]}/ep/talks/{published.filename}",
             {"Range": "bytes=-50"})
    assert r.status == 206 and r.read() == BODY[-50:]


def test_a_range_past_the_end_is_416(server, published):
    try:
        _get(f"{server[0]}/ep/talks/{published.filename}",
             {"Range": "bytes=99999-"})
        raise AssertionError("expected 416")
    except urllib.error.HTTPError as e:
        assert e.code == 416
        assert e.headers["Content-Range"] == f"bytes */{len(BODY)}"


def test_multiple_ranges_fall_back_to_the_whole_file(server, published):
    r = _get(f"{server[0]}/ep/talks/{published.filename}",
             {"Range": "bytes=0-9,20-29"})
    assert r.status == 200 and r.read() == BODY


@pytest.mark.parametrize("name", [
    "../../../etc/passwd",
    "..%2f..%2fsecret.mp3",
    "notahash.mp3",
    "0123456789abcdef.mp3.exe",
])
def test_an_episode_name_that_is_not_ours_is_404(server, published, name):
    assert _status(f"{server[0]}/ep/talks/{name}") == 404


def test_a_symlink_out_of_the_feed_is_not_served(server, spool, published, tmp_path):
    secret = tmp_path / "secret.mp3"
    secret.write_bytes(b"not yours")
    link = feed.feed_dir("talks") / "0000000000000000.mp3"
    link.symlink_to(secret)
    assert _status(f"{server[0]}/ep/talks/0000000000000000.mp3") == 404


# --- startup ---------------------------------------------------------------

def test_binding_off_loopback_without_a_token_is_a_startup_failure(monkeypatch):
    monkeypatch.delenv("MEDIA_FEED_TOKEN", raising=False)
    monkeypatch.setattr("agent_media_core.intake._env.load_env_file",
                        lambda *a, **k: None)
    assert feed_server.main(["--bind", "0.0.0.0", "--port", "0"]) == 2
