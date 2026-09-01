"""The feed spool: custody of rendered audio, and the XML a client subscribes to.

The property under test throughout is that the spool is the database — the
sidecar and the audio it describes are one thing, and nothing here may leave a
listing pointing at audio that isn't there (the failure the clip cache
already has).
"""

import json
import time
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import pytest

from agent_media_core import feed


@pytest.fixture(autouse=True)
def _spool(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_FEED_SPOOL", str(tmp_path / "spool"))
    # No config file: policies come from the defaults unless a test says so.
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "nope.toml"))
    return tmp_path


def _audio(tmp_path, name="clip.mp3", body=b"ID3fake-audio-bytes"):
    p = tmp_path / name
    p.write_bytes(body)
    return p


def _pub(tmp_path, feed_name="talks", guid="s-1", title="A talk", **kw):
    kw.setdefault("duration_s", 61.0)
    return feed.publish(feed_name, _audio(tmp_path), guid=guid, title=title, **kw)


# --- custody ---------------------------------------------------------------

def test_publish_copies_so_the_cache_may_still_sweep_the_original(tmp_path):
    src = _audio(tmp_path)
    ep = _pub(tmp_path, guid="s-1")
    src.unlink()                                  # the cache does this
    assert feed.episodes("talks") == [ep]
    assert (feed.feed_dir("talks") / ep.filename).read_bytes()


def test_publishing_the_same_guid_twice_replaces_rather_than_duplicates(tmp_path):
    _pub(tmp_path, guid="s-1", title="first")
    _pub(tmp_path, guid="s-1", title="second")
    eps = feed.episodes("talks")
    assert [e.title for e in eps] == ["second"]


def test_republishing_in_another_format_leaves_no_orphan_audio(tmp_path):
    _pub(tmp_path, guid="s-1")
    feed.publish("talks", _audio(tmp_path, "clip.m4a"), guid="s-1",
                 title="again", duration_s=1.0)
    d = feed.feed_dir("talks")
    assert sorted(p.suffix for p in d.iterdir()) == [".json", ".m4a"]


def test_episodes_are_newest_first(tmp_path):
    _pub(tmp_path, guid="old", title="old", published=1000.0)
    _pub(tmp_path, guid="new", title="new", published=2000.0)
    assert [e.title for e in feed.episodes("talks")] == ["new", "old"]


def test_an_episode_whose_audio_is_gone_is_not_listed(tmp_path):
    """A row that outlives its file is the exact failure this design exists to
    avoid; if it happens anyway, it must not be offered to a client."""
    ep = _pub(tmp_path, guid="s-1")
    (feed.feed_dir("talks") / ep.filename).unlink()
    assert feed.episodes("talks") == []


def test_an_unreadable_sidecar_drops_one_episode_not_the_feed(tmp_path):
    _pub(tmp_path, guid="good", title="good")
    bad = feed.feed_dir("talks") / "deadbeef.json"
    bad.write_text("{ not json")
    assert [e.title for e in feed.episodes("talks")] == ["good"]


def test_a_sidecar_naming_a_file_outside_the_feed_is_refused(tmp_path):
    ep = _pub(tmp_path, guid="s-1")
    side = feed.feed_dir("talks") / f"{ep.eid}.json"
    raw = json.loads(side.read_text())
    raw["filename"] = "../../secrets.mp3"
    side.write_text(json.dumps(raw))
    assert feed.episodes("talks") == []


def test_publish_refuses_empty_audio_and_a_missing_guid(tmp_path):
    empty = tmp_path / "empty.mp3"
    empty.write_bytes(b"")
    with pytest.raises(ValueError):
        feed.publish("talks", empty, guid="s-1", title="t")
    with pytest.raises(ValueError):
        feed.publish("talks", _audio(tmp_path), guid=" ", title="t")


@pytest.mark.parametrize("name", ["../etc", "talks/../..", "Talks!", ""])
def test_a_feed_name_cannot_leave_the_spool(name):
    assert not feed.valid_name(name)
    with pytest.raises(ValueError):
        feed.feed_dir(name)


def test_remove_takes_both_halves(tmp_path):
    _pub(tmp_path, guid="s-1")
    assert feed.remove("talks", "s-1") is True
    assert list(feed.feed_dir("talks").iterdir()) == []
    assert feed.remove("talks", "s-1") is False


# --- retention -------------------------------------------------------------

def test_gc_removes_by_age(tmp_path):
    now = 1_000_000.0
    _pub(tmp_path, guid="old", published=now - 91 * 86400)
    _pub(tmp_path, guid="new", published=now - 1 * 86400)
    assert feed.gc("talks", now=now) == ["old"]
    assert [e.guid for e in feed.episodes("talks")] == ["new"]


def test_gc_removes_by_count_after_age(tmp_path):
    now = 1_000_000.0
    for i in range(5):
        _pub(tmp_path, guid=f"e{i}", published=now - i * 3600)
    got = feed.gc("talks", now=now, pol=feed.Policy(keep_max=2))
    assert sorted(got) == ["e2", "e3", "e4"]
    assert [e.guid for e in feed.episodes("talks")] == ["e0", "e1"]


def test_an_empty_policy_never_deletes(tmp_path):
    now = 1_000_000.0
    _pub(tmp_path, feed_name="docs", guid="ancient", published=0.0)
    assert feed.gc("docs", now=now) == []
    assert len(feed.episodes("docs")) == 1


def test_policy_comes_from_config_and_a_bad_value_keeps_the_default(tmp_path,
                                                                   monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text('[feeds.talks]\nkeep_days = 7\n\n'
                 '[feeds.digest]\nkeep_days = "soon"\n')
    monkeypatch.setenv("MEDIA_CONFIG", str(p))
    assert feed.policy("talks").keep_days == 7
    assert feed.policy("digest").keep_days == 7        # the default, not the string
    # An unconfigured feed is a conversation feed — one gets created the first
    # time something is published from a new tmux workspace — so it inherits
    # `talks` rather than keeping everything forever.
    assert feed.policy("unheard-of") == feed.DEFAULT_POLICIES["talks"]


# --- the XML ---------------------------------------------------------------

def _channel(xml):
    return ET.fromstring(xml).find("channel")


def test_the_token_reaches_the_enclosures_not_just_the_feed(tmp_path):
    """A capability URL that authorises only the XML gives a client a feed
    that loads and episodes that all 401."""
    ep = _pub(tmp_path, guid="s-1")
    xml = feed.feed_xml("talks", [ep], base_url="http://red5:8782", token="s3cret")
    url = _channel(xml).find("item/enclosure").attrib["url"]
    assert url == "http://red5:8782/ep/talks/%s?k=s3cret" % ep.filename
    self_link = _channel(xml).find(
        "{http://www.w3.org/2005/Atom}link").attrib["href"]
    assert self_link.endswith("/feed/talks.xml?k=s3cret")


def test_no_token_means_no_query_string(tmp_path):
    ep = _pub(tmp_path, guid="s-1")
    xml = feed.feed_xml("talks", [ep], base_url="http://red5:8782/")
    assert _channel(xml).find("item/enclosure").attrib["url"] == \
        "http://red5:8782/ep/talks/%s" % ep.filename


def test_enclosure_declares_the_real_byte_length(tmp_path):
    ep = _pub(tmp_path, guid="s-1")
    xml = feed.feed_xml("talks", [ep], base_url="http://x")
    enc = _channel(xml).find("item/enclosure")
    assert int(enc.attrib["length"]) == ep.size > 0
    assert enc.attrib["type"] == "audio/mpeg"


def test_dates_are_rfc_2822(tmp_path):
    ep = _pub(tmp_path, guid="s-1", published=1_700_000_000.0)
    xml = feed.feed_xml("talks", [ep], base_url="http://x", now=1_700_000_100.0)
    ch = _channel(xml)
    assert parsedate_to_datetime(ch.find("item/pubDate").text).timestamp() == \
        1_700_000_000.0
    assert parsedate_to_datetime(ch.find("lastBuildDate").text)


def test_the_guid_is_not_a_permalink(tmp_path):
    ep = _pub(tmp_path, guid="/home/ryer/org/note.org")
    xml = feed.feed_xml("talks", [ep], base_url="http://x")
    g = _channel(xml).find("item/guid")
    assert g.attrib["isPermaLink"] == "false"
    assert g.text == "/home/ryer/org/note.org"


def test_a_description_that_closes_its_own_cdata_does_not_break_the_feed(tmp_path):
    ep = _pub(tmp_path, guid="s-1",
              description="he said ]]> and then <b>more</b> & more")
    xml = feed.feed_xml("talks", [ep], base_url="http://x")
    desc = _channel(xml).find("item/description").text
    assert desc == "he said ]]> and then <b>more</b> & more"


def test_a_title_with_markup_is_escaped(tmp_path):
    ep = _pub(tmp_path, guid="s-1", title="fixing <ringer> & duck")
    xml = feed.feed_xml("talks", [ep], base_url="http://x")
    assert _channel(xml).find("item/title").text == "fixing <ringer> & duck"


def test_duration_is_hms_and_omitted_when_unknown(tmp_path):
    itunes = "{http://www.itunes.com/dtds/podcast-1.0.dtd}duration"
    ep = _pub(tmp_path, guid="s-1", duration_s=3725.4)
    assert _channel(feed.feed_xml("talks", [ep], base_url="http://x")
                    ).find(f"item/{itunes}").text == "1:02:05"
    ep0 = _pub(tmp_path, guid="s-2", duration_s=0.0)
    assert _channel(feed.feed_xml("talks", [ep0], base_url="http://x")
                    ).find(f"item/{itunes}") is None


def test_an_empty_feed_is_still_valid_xml():
    xml = feed.feed_xml("talks", [], base_url="http://x", now=0.0)
    assert _channel(xml).find("title") is not None
    assert _channel(xml).find("item") is None


def test_write_feed_regenerates_from_the_sidecars_on_disk(tmp_path):
    _pub(tmp_path, guid="s-1", title="one")
    _pub(tmp_path, guid="s-2", title="two")
    path = feed.write_feed("talks", base_url="http://x", token="t")
    titles = [e.text for e in _channel(path.read_text()).findall("item/title")]
    assert sorted(titles) == ["one", "two"]
    assert feed.feeds() == ["talks"]


def test_feed_xml_touches_no_disk(tmp_path, monkeypatch):
    """It is the fiddly part, so it stays testable without a spool."""
    ep = feed.Episode(guid="g", title="t", filename="abc.mp3",
                      published=1.0, size=42)
    monkeypatch.setenv("MEDIA_FEED_SPOOL", "/nonexistent/nope")
    assert "abc.mp3" in feed.feed_xml("talks", [ep], base_url="http://x", now=1.0)


def test_the_channel_is_named_for_the_feed_alone(tmp_path):
    """One feed per workspace means a subscription list; a shared prefix on
    every entry spends its first fourteen characters saying nothing."""
    ep = _pub(tmp_path, feed_name="p-agent-media", guid="s-1")
    xml = feed.feed_xml("p-agent-media", [ep], base_url="http://x")
    assert _channel(xml).find("title").text == "p-agent-media"
