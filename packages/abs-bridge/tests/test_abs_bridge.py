"""The decisions inside the Audiobookshelf bridges.

These daemons are two poll loops; what is worth testing is what they decide.
Both ran untracked for months, so this is also the first test either has had —
the cases below are the ones that were only ever verified by watching a log.
"""

import pytest

from agent_media_abs import _abs, cast_watcher
from agent_media_abs.book_bridge import should_push


# --- which library ----------------------------------------------------------

LIBS = [{"id": "pod1", "name": "Spoken", "mediaType": "podcast"},
        {"id": "bk1", "name": "Audiobooks", "mediaType": "book"},
        {"id": "bk2", "name": "Kids", "mediaType": "book"}]


def test_the_first_book_library_wins_not_the_first_library():
    """A podcast library at the top of the list must not collect audiobook
    positions — the feed put one there, and it sorts first."""
    assert _abs.pick_library(LIBS) == "bk1"


@pytest.mark.parametrize("want", ["Kids", "bk2"])
def test_a_named_library_is_honoured_by_name_or_id(want):
    assert _abs.pick_library(LIBS, want) == "bk2"


def test_no_libraries_is_none_not_a_crash():
    assert _abs.pick_library([]) is None


# --- matching a file --------------------------------------------------------

ITEMS = [{"id": "i1", "relPath": "Hounded.m4b",
          "media": {"duration": 3600.0,
                    "audioFiles": [{"metadata": {"path": "/audiobooks/Hounded.m4b"}}]}}]


def test_files_are_matched_by_basename_across_the_container_boundary():
    """mpv sees /home/ryer/audiobooks/X; ABS, inside a container, sees
    /audiobooks/X. The filename is the only part both agree on."""
    m = _abs.basename_map(ITEMS)
    assert m["Hounded.m4b"] == {"id": "i1", "duration": 3600.0}


def test_an_item_with_no_audio_files_still_maps_by_its_relpath():
    m = _abs.basename_map([{"id": "i2", "relPath": "Deep/Scourged.m4b", "media": {}}])
    assert m["Scourged.m4b"]["id"] == "i2"


def test_local_path_takes_only_the_basename(tmp_path):
    """A path from the server is not a path to hand a player."""
    (tmp_path / "Hounded.m4b").write_bytes(b"x")
    assert _abs.local_path("/audiobooks/Hounded.m4b", lib=tmp_path) == \
        tmp_path / "Hounded.m4b"
    assert _abs.local_path("/etc/../audiobooks/Hounded.m4b", lib=tmp_path) == \
        tmp_path / "Hounded.m4b"
    assert _abs.local_path("/audiobooks/missing.m4b", lib=tmp_path) is None
    assert _abs.local_path("", lib=tmp_path) is None


# --- pushing a position -----------------------------------------------------

@pytest.mark.parametrize("prev,pos,want", [
    (None, 0.0, True),        # never pushed for this item
    (100.0, 110.0, True),     # a poll's worth of listening
    (100.0, 100.5, False),    # paused: the same number, again
    (100.0, 90.0, True),      # jumped back — a seek is worth recording
])
def test_only_real_movement_is_pushed(prev, pos, want):
    assert should_push(prev, pos, poll_s=10.0) is want


# --- is that session actually playing? --------------------------------------

@pytest.mark.parametrize("delta,elapsed,want", [
    (4.1, 4.0, True),       # playing at 1x
    (6.5, 4.0, True),       # playing sped up
    (0.0, 4.0, False),      # an idle tab, open for hours
    (0.2, 4.0, False),      # clock jitter, not playback
    (900.0, 4.0, False),    # a seek
    (-30.0, 4.0, False),    # jumped back
])
def test_only_a_real_time_advance_counts_as_playback(delta, elapsed, want):
    """This is what stops a forgotten tab seizing the speakers."""
    assert cast_watcher.is_advancing(delta, elapsed) is want


# --- device filtering -------------------------------------------------------

SESSION = {"deviceInfo": {"deviceName": "Pixel 8a", "clientName": "Abs Android"}}


def test_deny_filters_and_allow_overrides_it():
    assert cast_watcher.device_ok(SESSION, [], []) is True
    assert cast_watcher.device_ok(SESSION, [], ["pixel"]) is False
    assert cast_watcher.device_ok(SESSION, ["pixel"], ["pixel"]) is True
    assert cast_watcher.device_ok(SESSION, ["ipad"], []) is False


def test_a_session_with_no_device_info_is_still_labelled():
    assert cast_watcher.device_label({}) == "? / ?"


# --- casting ----------------------------------------------------------------

class _Abs:
    def __init__(self, item):
        self.item, self.closed = item, []

    def req(self, method, path, body=None):
        if path.startswith("/api/items/"):
            return self.item
        if path.endswith("/close"):
            self.closed.append(path)
        return {}


def _session():
    return {"id": "s1", "libraryItemId": "i1", "currentTime": 120.0,
            "displayTitle": "Hounded", "deviceInfo": {}}


def test_casting_plays_the_local_file_at_the_live_position(tmp_path, monkeypatch):
    (tmp_path / "Hounded.m4b").write_bytes(b"x")
    monkeypatch.setenv("AUDIOBOOK_LIB", str(tmp_path))
    calls = []
    monkeypatch.setattr(cast_watcher.subprocess, "call",
                        lambda cmd, **kw: calls.append(cmd) or 0)
    api = _Abs(ITEMS[0])
    assert cast_watcher.cast(api, _session()) is True
    assert calls[0][:3] == ["media", "book", "play"]
    assert calls[0][-2:] == ["--start-ms", "120000"]
    assert api.closed                       # the client is told to let go


def test_a_failed_handover_leaves_the_phone_playing(tmp_path, monkeypatch):
    """Closing the session after a failed cast stops the phone too — the one
    outcome worse than not casting."""
    (tmp_path / "Hounded.m4b").write_bytes(b"x")
    monkeypatch.setenv("AUDIOBOOK_LIB", str(tmp_path))
    monkeypatch.setattr(cast_watcher.subprocess, "call", lambda cmd, **kw: 1)
    api = _Abs(ITEMS[0])
    assert cast_watcher.cast(api, _session()) is False
    assert not api.closed


def test_a_file_the_rooms_cannot_reach_is_not_cast(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIOBOOK_LIB", str(tmp_path))     # empty library
    monkeypatch.setattr(cast_watcher.subprocess, "call",
                        lambda *a, **k: pytest.fail("must not play"))
    api = _Abs(ITEMS[0])
    assert cast_watcher.cast(api, _session()) is False
    assert not api.closed


def test_dry_run_decides_but_touches_nothing(tmp_path, monkeypatch):
    (tmp_path / "Hounded.m4b").write_bytes(b"x")
    monkeypatch.setenv("AUDIOBOOK_LIB", str(tmp_path))
    monkeypatch.setattr(cast_watcher.subprocess, "call",
                        lambda *a, **k: pytest.fail("must not play"))
    api = _Abs(ITEMS[0])
    assert cast_watcher.cast(api, _session(), dry=True) is True
    assert not api.closed


# --- config -----------------------------------------------------------------

def test_the_environment_beats_the_config_file(tmp_path, monkeypatch):
    cfg = tmp_path / "abs-bridge.env"
    cfg.write_text('# comment\nABS_URL="http://from-file"\nABS_TOKEN=filetok\n\n')
    monkeypatch.setenv("ABS_URL", "http://from-env")
    monkeypatch.delenv("ABS_TOKEN", raising=False)
    _abs.load_env(cfg)
    assert _abs.Abs().url == "http://from-env"
    assert _abs.Abs().token == "filetok"


def test_a_missing_config_file_is_not_an_error(tmp_path):
    _abs.load_env(tmp_path / "nope.env")


# --- keeping Audiobookshelf in step with the feeds --------------------------
# Conversations publish into a feed per tmux workspace, so the set of feeds
# grows on its own. Subscribing by hand is the chore that gets done twice and
# then stops.

from agent_media_abs import sync as syncmod


def test_a_rotated_token_is_not_a_new_podcast():
    """ABS matches feeds by URL and ours carry a capability token; comparing
    the whole URL would subscribe the same feed twice after a rotation."""
    a = "http://red5:8782/feed/talks.xml?k=old"
    b = "http://red5:8782/feed/talks.xml?k=new-and-longer"
    assert syncmod.feed_key(a) == syncmod.feed_key(b)
    assert syncmod.feed_key(a) != syncmod.feed_key("http://red5:8782/feed/docs.xml")


def test_episodes_are_matched_by_guid_then_by_enclosure():
    have = [{"guid": "session:abc"}, {"enclosure": {"url": "http://h/ep/x.mp3?k=t"}}]
    feed_eps = [{"guid": "session:abc"},                       # already there
                {"enclosure": {"url": "http://h/ep/x.mp3?k=DIFFERENT"}},
                {"guid": "session:new"}]
    missing = syncmod.missing_episodes(feed_eps, have)
    assert [e["guid"] for e in missing] == ["session:new"]


class _AbsFake:
    def __init__(self, libraries=(), items=(), episodes=None):
        self.libraries = list(libraries)
        self.items = list(items)
        self.episodes = episodes or {}
        self.calls = []

    def req(self, method, path, body=None):
        self.calls.append((method, path.split("?")[0], body))
        if path.startswith("/api/libraries/"):
            return {"results": self.items}
        if path == "/api/libraries" and method == "GET":
            return {"libraries": self.libraries}
        if path == "/api/libraries" and method == "POST":
            lib = {"id": "newlib", "mediaType": "podcast",
                   "folders": [{"id": "f1", "fullPath": body["folders"][0]["fullPath"]}]}
            self.libraries.append(lib)
            return {"library": lib}
        if path == "/api/podcasts/feed":
            return {"podcast": {"metadata": {"title": "agent-media: talks"},
                                "episodes": [{"guid": "session:a"},
                                             {"guid": "session:b"}]}}
        if path == "/api/podcasts":
            return {"id": "item1"}
        if path.startswith("/api/items/"):
            return {"media": {"episodes": self.episodes.get("item1", []),
                              "autoDownloadEpisodes": True}}
        return {}


LIB = [{"id": "lib1", "mediaType": "podcast",
        "folders": [{"id": "f1", "fullPath": "/audiobooks/podcasts"}]}]
FEEDS = [("talks", "http://red5:8782/feed/talks.xml?k=t")]


def test_an_unknown_feed_is_subscribed_and_backfilled():
    """ABS never backfills on its own: autoDownloadEpisodes only catches what
    appears after subscribing."""
    api = _AbsFake(libraries=LIB)
    assert syncmod.sync(api, FEEDS, folder_path="/audiobooks/podcasts") > 0
    posted = [c for c in api.calls if c[0] == "POST"]
    assert any(c[1] == "/api/podcasts" for c in posted)
    dl = next(c for c in posted if c[1].endswith("/download-episodes"))
    assert [e["guid"] for e in dl[2]] == ["session:a", "session:b"]


def test_a_feed_already_subscribed_and_current_changes_nothing():
    api = _AbsFake(
        libraries=LIB,
        items=[{"id": "item1",
                "media": {"metadata": {"feedUrl": "http://red5:8782/feed/talks.xml?k=OLD"}}}],
        episodes={"item1": [{"guid": "session:a"}, {"guid": "session:b"}]})
    assert syncmod.sync(api, FEEDS, folder_path="/audiobooks/podcasts") == 0
    assert not [c for c in api.calls if c[1] == "/api/podcasts"]


def test_a_subscribed_feed_that_has_grown_is_topped_up():
    api = _AbsFake(
        libraries=LIB,
        items=[{"id": "item1",
                "media": {"metadata": {"feedUrl": "http://red5:8782/feed/talks.xml?k=t"}}}],
        episodes={"item1": [{"guid": "session:a"}]})
    assert syncmod.sync(api, FEEDS, folder_path="/audiobooks/podcasts") == 1
    dl = next(c for c in api.calls if c[1].endswith("/download-episodes"))
    assert [e["guid"] for e in dl[2]] == ["session:b"]


def test_a_dry_run_writes_nothing():
    api = _AbsFake(libraries=LIB)
    syncmod.sync(api, FEEDS, folder_path="/audiobooks/podcasts", dry_run=True)
    assert not [c for c in api.calls if c[0] in ("POST", "PATCH")
                and c[1] not in ("/api/podcasts/feed",)]


def test_a_missing_podcast_library_is_created_once():
    api = _AbsFake(libraries=[{"id": "bk", "mediaType": "book", "folders": []}])
    syncmod.sync(api, FEEDS, folder_path="/audiobooks/podcasts")
    made = [c for c in api.calls if c[0] == "POST" and c[1] == "/api/libraries"]
    assert len(made) == 1
    assert made[0][2]["mediaType"] == "podcast"


def test_a_feed_name_that_is_not_a_directory_name():
    assert syncmod.safe_dirname("agent-media: talks") == "agent-media_ talks"
    assert syncmod.safe_dirname("../etc") == ".._etc"


def test_an_empty_feed_is_not_subscribed_to():
    """A workspace that has published nothing yet is a subscription to
    nothing — and ABS refuses to parse the feed anyway."""
    class _Empty(_AbsFake):
        def req(self, method, path, body=None):
            if path == "/api/podcasts/feed":
                self.calls.append((method, path, body))
                return {"podcast": {"metadata": {}, "episodes": []}}
            return super().req(method, path, body)

    api = _Empty(libraries=LIB)
    assert syncmod.sync(api, FEEDS, folder_path="/audiobooks/podcasts") == 0
    assert not [c for c in api.calls if c[1] == "/api/podcasts"]


def test_one_unreadable_feed_does_not_end_the_run():
    """This walks every workspace; the newest is the likeliest to be empty or
    half-written, and it must not take the others down with it."""
    class _Flaky(_AbsFake):
        def req(self, method, path, body=None):
            if path == "/api/podcasts/feed" and "bad" in (body or {}).get("rssFeed", ""):
                raise OSError("404: Podcast RSS feed request failed")
            return super().req(method, path, body)

    api = _Flaky(libraries=LIB)
    feeds = [("bad", "http://red5:8782/feed/bad.xml?k=t")] + FEEDS
    assert syncmod.sync(api, feeds, folder_path="/audiobooks/podcasts") > 0
    assert any(c[1] == "/api/podcasts" for c in api.calls)
