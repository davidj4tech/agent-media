"""Publishing a document to a feed.

The docs channel already renders a chaptered mp3; publishing is custody of
that file plus a row, and nothing else. So what is worth testing is the
contract around it — that publishing does not also play, that the guid is the
document (so an edited doc replaces its episode rather than appearing twice),
and that a subscriber is given the shape of the document to decide from.
"""

import pytest

from agent_media_core import cli, docs, feed


@pytest.fixture(autouse=True)
def _spool(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_FEED_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "nope.toml"))
    monkeypatch.delenv("MEDIA_FEED_BASE_URL", raising=False)
    monkeypatch.delenv("MEDIA_FEED_TOKEN", raising=False)


@pytest.fixture
def doc_root(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "ringer.md").write_text(
        "# Silencing the ringer\n\nStatus: proposal\n\n"
        "The phone talks at 08:45 whether or not it is meant to.\n\n"
        "## The gate\n\nBeside the mute check.\n")
    monkeypatch.setenv("MEDIA_DOC_ROOTS", str(root))
    return root


@pytest.fixture
def rendered(tmp_path, monkeypatch):
    """A stand-in for the TTS render: the same contract, none of the seconds."""
    clip = tmp_path / "render.mp3"
    clip.write_bytes(b"ID3" + b"audio" * 200)
    monkeypatch.setattr(docs, "render_doc", lambda *a, **k: clip)
    monkeypatch.setattr(docs, "render_sections", lambda *a, **k: clip)
    # ffprobe on a fake mp3 is a subprocess for no answer.
    monkeypatch.setattr(feed, "_probe_duration", lambda p: 61.0)
    return clip


class _NoPlayer:
    """Any call here is a failure: publishing is instead of playing."""

    def __getattr__(self, name):
        def _boom(*a, **k):
            raise AssertionError(f"publishing must not call {name}")
        return _boom


def _run(monkeypatch, argv, srv=None):
    monkeypatch.setattr(cli, "_srv", lambda: srv or _NoPlayer())
    return cli.main(argv)


# --- publishing a document -------------------------------------------------

def test_publishing_a_doc_does_not_also_play_it(monkeypatch, doc_root, rendered,
                                                capsys):
    assert _run(monkeypatch, ["doc", "play", "ringer", "--feed"]) == 0
    eps = feed.episodes("docs")
    assert [e.title for e in eps] == ["Silencing the ringer"]
    assert "Silencing the ringer" in capsys.readouterr().out


def test_the_feed_name_defaults_to_docs_and_can_be_named(monkeypatch, doc_root,
                                                         rendered):
    assert _run(monkeypatch, ["doc", "play", "ringer", "--feed", "talks"]) == 0
    assert feed.episodes("docs") == []
    assert len(feed.episodes("talks")) == 1


def test_republishing_an_edited_doc_replaces_its_episode(monkeypatch, doc_root,
                                                         rendered):
    """Two versions of one document in a client is the shape of confusion
    nobody unpicks."""
    _run(monkeypatch, ["doc", "play", "ringer", "--feed"])
    (doc_root / "ringer.md").write_text("# Silencing the ringer, again\n\nNew.\n")
    _run(monkeypatch, ["doc", "play", "ringer", "--feed"])
    eps = feed.episodes("docs")
    assert len(eps) == 1
    assert eps[0].title == "Silencing the ringer, again"
    assert eps[0].guid == str((doc_root / "ringer.md").resolve())


def test_the_subscriber_is_given_the_shape_of_the_document(monkeypatch,
                                                           doc_root, rendered):
    _run(monkeypatch, ["doc", "play", "ringer", "--feed"])
    desc = feed.episodes("docs")[0].description
    assert "08:45" in desc                       # the opening prose
    assert "Chapters:" in desc and "<p>The gate</p>" in desc


def test_without_the_flag_it_still_plays(monkeypatch, doc_root, rendered):
    class _Player:
        def __init__(self):
            self.calls = []

        def book_play(self, clip, **kw):
            self.calls.append(clip)
            return {"ok": True}

    srv = _Player()
    assert _run(monkeypatch, ["doc", "play", "ringer"], srv=srv) == 0
    assert srv.calls and feed.feeds() == []


def test_a_missing_document_publishes_nothing(monkeypatch, doc_root, rendered):
    assert _run(monkeypatch, ["doc", "play", "nosuchdoc", "--feed"]) == 1
    assert feed.feeds() == []


def test_the_feed_xml_is_written_when_a_base_url_is_configured(
        monkeypatch, doc_root, rendered):
    monkeypatch.setenv("MEDIA_FEED_BASE_URL", "http://red5:8782")
    monkeypatch.setenv("MEDIA_FEED_TOKEN", "s3cret")
    _run(monkeypatch, ["doc", "play", "ringer", "--feed"])
    xml = (feed.feed_dir("docs") / "feed.xml").read_text()
    assert "?k=s3cret" in xml and "Silencing the ringer" in xml


def test_no_base_url_still_publishes(monkeypatch, doc_root, rendered):
    """The spool is the thing that matters; the XML is regenerable.

    The base URL is neutralised at the function rather than in the
    environment: `media` layers ~/.config/agent-media.env over anything unset,
    so on a host that has one configured (every deployed host) `delenv` is
    undone the moment the CLI starts — a test that passes or fails by which
    machine it runs on.
    """
    monkeypatch.setattr(cli, "_feed_base_url", lambda: "")
    assert _run(monkeypatch, ["doc", "play", "ringer", "--feed"]) == 0
    assert len(feed.episodes("docs")) == 1
    assert not (feed.feed_dir("docs") / "feed.xml").exists()


# --- stdin and the agenda --------------------------------------------------

def test_a_region_from_an_editor_is_keyed_on_its_text(monkeypatch, rendered,
                                                      capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("# Note\n\nA thought.\n"))
    assert _run(monkeypatch, ["doc", "play", "--stdin", "--title", "A thought",
                              "--feed"]) == 0
    eps = feed.episodes("docs")
    assert len(eps) == 1 and eps[0].guid.startswith("stdin:")


def test_the_agenda_is_one_episode_per_day(monkeypatch, rendered):
    import time as _t
    from agent_media_core import agenda as ag

    monkeypatch.setattr(ag, "load_entries", lambda *a, **k: [])
    monkeypatch.setattr(ag, "agenda_sections",
                        lambda *a, **k: [docs.Section("Today", "Two things.")])
    assert _run(monkeypatch, ["doc", "agenda", "--feed"]) == 0
    eps = feed.episodes("digest")
    assert len(eps) == 1
    assert eps[0].guid == "agenda:" + _t.strftime("%Y-%m-%d")

    _run(monkeypatch, ["doc", "agenda", "--feed"])       # the same morning
    assert len(feed.episodes("digest")) == 1


# --- the notes themselves --------------------------------------------------

def test_episode_notes_break_at_a_sentence(monkeypatch):
    long = "One sentence here. " + "Another one follows. " * 40
    secs = [docs.Section("H", long), docs.Section("Later", "x")]
    notes = docs.episode_notes(secs, intro_chars=120)
    assert notes.startswith("<p>One sentence here.")
    intro = notes.splitlines()[0]
    assert intro.endswith("…</p>")
    assert "<p>Chapters:</p>" in notes
    assert "<p>H</p>" in notes and "<p>Later</p>" in notes


def test_episode_notes_of_a_headingless_note():
    assert docs.episode_notes([docs.Section("", "Just prose.")]) == "<p>Just prose.</p>"
