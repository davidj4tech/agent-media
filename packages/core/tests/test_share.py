"""`media share`: URL extraction, classification, dispatch.

The classifier is the point of this feature — a share sheet gives us a link and
we decide, unsupervised, whether speech should duck under it or pause for it.
Every rule in `share.classify` has a case here, because a heuristic nobody can
test is a heuristic nobody can change.
"""

import json

import pytest

from agent_media_core import share


def _p(**kw) -> share.Probe:
    """A probed Probe — the classifier treats `probed=False` as a special case
    (URL shape only), so the default here has to be the confident one."""
    kw.setdefault("url", "https://www.youtube.com/watch?v=jNQXAC9IVRw")
    kw.setdefault("probed", True)
    return share.Probe(**kw)


# ---- extracting the link out of what a share sheet actually sends ---------

@pytest.mark.parametrize("text,want", [
    ("https://youtu.be/jNQXAC9IVRw", "https://youtu.be/jNQXAC9IVRw"),
    # YouTube's own share: title, newline, link.
    ("Me at the zoo\nhttps://youtu.be/jNQXAC9IVRw",
     "https://youtu.be/jNQXAC9IVRw"),
    ("check this out https://example.com/a.mp3 it's great",
     "https://example.com/a.mp3"),
    # Trailing sentence punctuation is not part of the URL.
    ("listen: https://example.com/a.mp3.", "https://example.com/a.mp3"),
    ("(https://example.com/a.mp3)", "https://example.com/a.mp3"),
    ("  https://example.com/x?v=1&t=2s  ", "https://example.com/x?v=1&t=2s"),
])
def test_extract_url(text, want):
    assert share.extract_url(text) == want


def test_extract_url_without_a_link_is_a_share_error():
    with pytest.raises(share.ShareError):
        share.extract_url("no link here at all")


# ---- hosts that settle it without any metadata ---------------------------

@pytest.mark.parametrize("url,channel,ct", [
    ("https://podcasts.apple.com/au/podcast/x/id123", "book", "podcast"),
    ("https://pca.st/episode/abc", "book", "podcast"),
    ("https://www.audible.com.au/pd/x", "book", "audiobook"),
    ("https://librivox.org/the-odyssey/", "book", "audiobook"),
    ("https://soundcloud.com/artist/track", "music", "music"),
    ("https://music.youtube.com/watch?v=abc", "music", "music"),
    ("https://artist.bandcamp.com/album/x", "music", "music"),
])
def test_host_decides(url, channel, ct):
    v = share.classify(_p(url=url))
    assert (v.channel, v.content_type) == (channel, ct)


@pytest.mark.parametrize("url,host", [
    ("https://www.youtube.com/watch?v=x", "youtube.com"),
    ("https://youtube.com/watch?v=x", "youtube.com"),
    # A host whose own first characters are in "www." — the reason this is
    # removeprefix and not lstrip.
    ("https://wired.com/story/x", "wired.com"),
    ("https://www.w3.org/x", "w3.org"),
])
def test_host_strips_only_the_www_prefix(url, host):
    assert share.Probe(url=url).host == host


def test_spotify_episode_is_a_podcast_but_a_track_is_music():
    ep = share.classify(_p(url="https://open.spotify.com/episode/abc"))
    tr = share.classify(_p(url="https://open.spotify.com/track/abc"))
    assert ep.channel == "book" and ep.content_type == "podcast"
    assert tr.channel == "music"


def test_a_music_host_beats_a_long_duration():
    # An hour-long Bandcamp album is still music: it should duck, not pause.
    v = share.classify(_p(url="https://artist.bandcamp.com/album/x",
                          duration_s=3600))
    assert v.channel == "music"


# ---- what the metadata says ----------------------------------------------

def test_live_stream_is_ambient_music():
    v = share.classify(_p(live=True, duration_s=None))
    assert (v.channel, v.content_type) == ("music", "ambient")


@pytest.mark.parametrize("uploader", ["Björk - Topic", "taylorswiftVEVO"])
def test_artist_channels_are_music_at_any_length(uploader):
    # A full album upload on a Topic channel runs past the longform gate and
    # must not be mistaken for a lecture.
    v = share.classify(_p(uploader=uploader, duration_s=4200))
    assert v.channel == "music"


def test_long_music_category_is_a_dj_set_not_a_book():
    v = share.classify(_p(categories=["Music"], duration_s=7200))
    assert (v.channel, v.content_type) == ("music", "dj-set")
    assert "2h" in v.reason


def test_short_music_category_is_plain_music():
    v = share.classify(_p(categories=["Music"], duration_s=210))
    assert (v.channel, v.content_type) == ("music", "music")


def test_long_anything_else_is_longform():
    v = share.classify(_p(categories=["Entertainment"], duration_s=9000))
    assert (v.channel, v.content_type) == ("book", "audiobook")


def test_long_and_talky_is_a_podcast():
    v = share.classify(_p(categories=["Science & Technology"], duration_s=5400))
    assert (v.channel, v.content_type) == ("book", "podcast")


def test_mid_length_talky_is_longform_too():
    # 20 minutes of "Education" is a lecture, not a track: it should pause.
    v = share.classify(_p(categories=["Education"], duration_s=1200))
    assert (v.channel, v.content_type) == ("book", "podcast")


def test_mid_length_untalky_stays_on_music():
    v = share.classify(_p(categories=["Entertainment"], duration_s=1200))
    assert v.channel == "music"


def test_short_clip_is_music():
    v = share.classify(_p(categories=["Comedy"], duration_s=95))
    assert (v.channel, v.content_type) == ("music", "music")


def test_the_longform_threshold_is_tunable():
    p = _p(categories=["Entertainment"], duration_s=1000)
    assert share.classify(p).channel == "music"
    assert share.classify(p, longform_s=900).channel == "book"


def test_an_unprobed_link_falls_back_to_music():
    v = share.classify(share.Probe(url="https://example.com/thing"))
    assert v.channel == "music"
    assert "no metadata" in v.reason


def test_a_link_with_no_audio_is_refused():
    with pytest.raises(share.ShareError):
        share.classify(_p(has_audio=False))


def test_an_unprobed_link_with_no_audio_is_not_refused():
    # `has_audio` defaults True and means nothing when the probe never ran —
    # refusing here would reject every link whenever yt-dlp is missing.
    v = share.classify(share.Probe(url="https://example.com/x", has_audio=False))
    assert v.channel == "music"


# ---- overrides ------------------------------------------------------------

def test_explicit_channel_wins_and_says_so():
    v = share.classify(_p(categories=["Music"], duration_s=200), channel="book")
    assert v.channel == "book"
    assert "overridden" in v.reason


def test_explicit_content_type_wins():
    v = share.classify(_p(categories=["Music"], duration_s=200),
                       content_type="audiobook")
    assert v.content_type == "audiobook"
    assert v.channel == "music"  # only what was overridden changes


# ---- the probe ------------------------------------------------------------

def test_probe_reads_the_fields_it_needs():
    meta = {"title": "A Talk", "duration": 3600.0, "categories": ["Education"],
            "channel": "Some Channel", "extractor_key": "Youtube",
            "live_status": "not_live", "formats": [{"acodec": "opus"}]}

    def fake(args, timeout):
        assert "--no-playlist" in args and "--skip-download" in args
        return json.dumps(meta)

    p = share.probe("https://youtu.be/x", runner=fake)
    assert p.probed and p.title == "A Talk" and p.duration_s == 3600.0
    assert p.categories == ["Education"] and p.uploader == "Some Channel"
    assert not p.live and p.has_audio


def test_probe_judges_a_playlist_by_its_first_entry():
    meta = {"entries": [{"title": "Part 1", "duration": 4000,
                         "categories": ["Education"], "formats": [{}]}]}
    p = share.probe("https://youtu.be/x", runner=lambda a, t: json.dumps(meta))
    assert p.title == "Part 1" and p.duration_s == 4000


def test_a_failed_probe_is_not_an_error():
    def boom(args, timeout):
        raise RuntimeError("yt-dlp exploded")

    p = share.probe("https://youtu.be/x", runner=boom)
    assert not p.probed and p.url == "https://youtu.be/x"
    # ...and it still classifies, which is the whole point of not raising.
    assert share.classify(p).channel == "music"


def test_a_non_json_probe_is_not_an_error():
    p = share.probe("https://youtu.be/x", runner=lambda a, t: "Sign in to confirm")
    assert not p.probed


# ---- dispatch: the CLI command a share becomes ----------------------------

def test_music_dispatch_passes_the_content_type():
    seen = []
    v = share.Verdict("music", "dj-set", "long set")
    share.dispatch("https://youtu.be/x", v, where="phone",
                   runner=lambda argv: seen.append(argv) or 0)
    assert seen == [["music", "play", "https://youtu.be/x",
                     "--as", "dj-set", "--where", "phone"]]


def test_book_dispatch_uses_the_book_command():
    seen = []
    v = share.Verdict("book", "podcast", "long")
    share.dispatch("https://youtu.be/x", v, where="phone",
                   runner=lambda argv: seen.append(argv) or 0)
    assert seen == [["book", "play", "https://youtu.be/x", "--target", "phone"]]


@pytest.mark.parametrize("where", ["", "default", "auto"])
def test_book_dispatch_leaves_the_default_target_alone(where):
    # The book channel has no listener-aware `auto`, so these mean "configured
    # default" rather than a target name it would reject.
    seen = []
    share.dispatch("u", share.Verdict("book", "audiobook", "long"), where=where,
                   runner=lambda argv: seen.append(argv) or 0)
    assert "--target" not in seen[0]


def test_dispatch_carries_a_known_title():
    # A share is the one play path that already knows the title; without it
    # `media recent` lists the row as a bare video id.
    seen = []
    v = share.Verdict("music", "music", "short", title="Me at the zoo")
    share.dispatch("u", v, runner=lambda argv: seen.append(argv) or 0)
    assert seen[0] == ["music", "play", "u", "--as", "music",
                       "--title", "Me at the zoo"]
    seen.clear()
    b = share.Verdict("book", "podcast", "long", title="Ep. 12")
    share.dispatch("u", b, runner=lambda argv: seen.append(argv) or 0)
    assert seen[0] == ["book", "play", "u", "--title", "Ep. 12"]


def test_dispatch_returns_the_commands_exit_code():
    rc = share.dispatch("u", share.Verdict("music", "music", "why"),
                        runner=lambda argv: 3)
    assert rc == 3
