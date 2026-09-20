"""The conversation's cover: the canvas picture that best represents it.

A shelf of identical default covers says nothing about what is on it. The
canvas has already drawn something for nearly every reply, so the library can
have the one that meant the most — a marked figure where there is one, the
latest artwork otherwise.
"""

import json

import pytest

from agent_media_core import book_tracks


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MEDIA_ABS_COVERS", raising=False)
    return tmp_path


def _spool():
    from agent_media_visual.state import spool_dir
    return spool_dir()


def _push(key, session, *, images, purpose=None, t=0.0):
    """Write one remembered /show payload, as the visual channel would."""
    from agent_media_visual.state import pushes_path
    path = pushes_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    payload = {"session": session, "purpose": purpose}
    if len(images) == 1:
        payload["image"] = images[0]
    else:
        payload["sequence"] = [{"image": n, "at": i / len(images)}
                               for i, n in enumerate(images)]
    data[key] = {"payload": payload, "t": t}
    path.write_text(json.dumps(data))
    for name in images:
        if "/" in name:          # another host's spool: no local file
            continue
        (_spool() / name).write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'"
                                      b" viewBox='0 0 4 4'></svg>")


def test_no_pictures_no_cover():
    assert book_tracks._cover_choice("sess") is None


def test_latest_artwork_when_there_is_no_figure():
    _push("k1", "sess", images=["a.svg"], t=10)
    _push("k2", "sess", images=["b.svg"], t=20)
    assert book_tracks._cover_choice("sess").name == "b.svg"


def test_a_figure_beats_later_artwork():
    """A [[visual:]] was drawn on purpose; ambient art was not."""
    _push("k1", "sess", images=["fig.svg"], purpose="figure", t=10)
    _push("k2", "sess", images=["art.svg"], t=99)
    assert book_tracks._cover_choice("sess").name == "fig.svg"


def test_the_newest_figure_wins_among_figures():
    _push("k1", "sess", images=["old.svg"], purpose="figure", t=10)
    _push("k2", "sess", images=["new.svg"], purpose="figure", t=20)
    assert book_tracks._cover_choice("sess").name == "new.svg"


def test_a_sequence_contributes_its_final_beat():
    """The last beat is the scene fully developed — what the canvas parks on."""
    _push("k1", "sess", images=["b1.svg", "b2.svg", "b3.svg"], t=10)
    assert book_tracks._cover_choice("sess").name == "b3.svg"


def test_other_sessions_are_not_borrowed_from():
    _push("k1", "other", images=["theirs.svg"], t=10)
    assert book_tracks._cover_choice("sess") is None


def test_a_swept_file_is_not_offered():
    """The spool is garbage-collected; the memory of what was pushed is not."""
    _push("k1", "sess", images=["gone.svg"], t=10)
    (_spool() / "gone.svg").unlink()
    assert book_tracks._cover_choice("sess") is None


def test_another_hosts_spool_is_skipped():
    _push("k1", "sess", images=["http://elsewhere/img/x.svg"], t=10)
    assert book_tracks._cover_choice("sess") is None


# --- turning the choice into something Audiobookshelf will take ---------------

def test_svg_is_rasterised(tmp_path):
    cairosvg = pytest.importorskip("cairosvg")
    assert cairosvg
    p = tmp_path / "card.svg"
    p.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 9">'
                 '<rect width="16" height="9" fill="#123"/></svg>')
    data, name, ctype = book_tracks._as_raster(p)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert name == "card.png" and ctype == "image/png"


def test_raster_formats_pass_through(tmp_path):
    p = tmp_path / "art.webp"
    p.write_bytes(b"RIFF....WEBP")
    assert book_tracks._as_raster(p) == (b"RIFF....WEBP", "art.webp", "image/webp")


def test_an_unknown_format_is_declined(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    assert book_tracks._as_raster(p) is None


def test_multipart_carries_the_file():
    body, ctype = book_tracks._multipart("cover", "a.png", "image/png", b"\x01\x02")
    assert ctype.startswith("multipart/form-data; boundary=")
    boundary = ctype.split("boundary=", 1)[1]
    assert body.startswith(f"--{boundary}\r\n".encode())
    assert b'name="cover"; filename="a.png"' in body
    assert b"\x01\x02" in body
    assert body.endswith(f"\r\n--{boundary}--\r\n".encode())


# --- the whole call -----------------------------------------------------------

def test_cover_is_off_when_asked(monkeypatch):
    monkeypatch.setenv("MEDIA_ABS_COVERS", "0")
    _push("k1", "sess", images=["a.svg"], t=10)
    assert book_tracks.set_cover("sess", book_tracks.root() / "w" / "c") == ""


def test_an_unchanged_cover_is_not_re_uploaded(monkeypatch):
    """Metadata is re-applied on every publish; a file upload should not be."""
    pytest.importorskip("cairosvg")
    _push("k1", "sess", images=["a.svg"], t=10)
    book_tracks._write_manifest("sess", {"cover": "a.svg"})
    calls = []
    monkeypatch.setattr(book_tracks, "_abs_ready_all",
                        lambda target=None: calls.append(1) or [])
    assert book_tracks.set_cover("sess", book_tracks.root() / "w" / "c") == ""
    assert not calls          # it never even looked for a server
