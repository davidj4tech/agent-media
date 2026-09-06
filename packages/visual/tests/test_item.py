"""The library item, slimmed for the app that reads it."""

import json
import urllib.error

import pytest

from agent_media_visual import item as item_mod


def _track(index=1, **over):
    t = {
        "index": index,
        "ino": "1609460",
        "metadata": {
            "filename": f"{index:04d} - a sentence.mp3",
            "ext": ".mp3",
            "path": "/conversations/x/0001 - a sentence.mp3",
            "relPath": "0001 - a sentence.mp3",
            "size": 25344,
            "mtimeMs": 1788526415154,
            "ctimeMs": 1788526602392,
            "birthtimeMs": 1788526412983,
        },
        "addedAt": 1788526603861,
        "updatedAt": 1788526603861,
        "startOffset": 4.2 * (index - 1),
        "duration": 4.224,
        "title": "a sentence",
        "contentUrl": "/s/item/x/0001.mp3",
        "mimeType": "audio/mpeg",
        "bitRate": 48000,
        "codec": "mp3",
        "channelLayout": "mono",
        "trackNumFromFilename": index,
    }
    t.update(over)
    return t


def _item(tracks=2, files=None):
    return {
        "id": "li_1",
        "mediaType": "book",
        "libraryFiles": files if files is not None else [
            {"ino": "1", "fileType": "audio", "metadata": {"size": 25344}},
            {"ino": "2", "fileType": "ebook", "metadata": {"size": 900}},
            {"ino": "3", "fileType": "image", "metadata": {"size": 400}},
        ],
        "media": {
            "id": "bk_1",
            "metadata": {"title": "A conversation"},
            "chapters": [{"id": 0, "start": 0, "title": "one"}],
            "audioFiles": [{"ino": "1"}, {"ino": "2"}],
            "tracks": [_track(i + 1) for i in range(tracks)],
        },
    }


# --- what comes out -----------------------------------------------------------

def test_audio_files_go():
    out = item_mod.slim_item(_item())
    assert "audioFiles" not in out["media"]


def test_audio_library_files_go_and_the_rest_stay():
    out = item_mod.slim_item(_item())
    kinds = [f["fileType"] for f in out["libraryFiles"]]
    # An audiobook with an epub still has to open in the reader, and the cover
    # is found through this list too.
    assert kinds == ["ebook", "image"]


def test_a_track_keeps_what_the_app_reads():
    out = item_mod.slim_item(_item(tracks=1))
    track = out["media"]["tracks"][0]
    assert track["index"] == 1
    assert track["startOffset"] == 0.0
    assert track["duration"] == 4.224
    assert track["title"] == "a sentence"
    assert track["contentUrl"] == "/s/item/x/0001.mp3"
    assert track["mimeType"] == "audio/mpeg"


def test_a_track_loses_the_facts_about_the_file_on_disk():
    track = item_mod.slim_item(_item(tracks=1))["media"]["tracks"][0]
    for gone in ("ino", "addedAt", "updatedAt", "bitRate", "channelLayout",
                 "trackNumFromFilename"):
        assert gone not in track
    # The filename came twice — as `title` and again inside `metadata` — so
    # the whole of `metadata` goes and `title` carries it.
    assert "metadata" not in track


def test_chapters_and_metadata_survive():
    out = item_mod.slim_item(_item())
    assert out["media"]["metadata"]["title"] == "A conversation"
    assert len(out["media"]["chapters"]) == 1
    assert out["id"] == "li_1"


def test_the_original_is_not_emptied():
    # The caller may still be holding the parsed response.
    original = _item()
    item_mod.slim_item(original)
    assert "audioFiles" in original["media"]
    assert len(original["libraryFiles"]) == 3
    assert "ino" in original["media"]["tracks"][0]


def test_it_is_much_smaller():
    # The point of the exercise, in the shape the measurement had: hundreds of
    # sentence-length tracks, each with its own library file.
    big = _item(tracks=400)
    big["libraryFiles"] = [
        {"ino": str(1609460 + i), "isSupplementary": None,
         "addedAt": 1788526602949, "updatedAt": 1788526602949,
         "fileType": "audio",
         "metadata": dict(_track(i + 1)["metadata"])}
        for i in range(400)]
    before = len(json.dumps(big))
    after = len(json.dumps(item_mod.slim_item(big)))
    # Measured on the real thing, whose titles are longer than these:
    # 1267 KB of item became 138 KB, 9.2x. This fixture manages 6.
    assert after < before / 6


def test_junk_is_passed_through_rather_than_crashing():
    assert item_mod.slim_item(None) is None
    assert item_mod.slim_item({"media": "not a dict"}) == {"media": "not a dict"}
    assert item_mod.slim_item({"libraryFiles": "no"})["libraryFiles"] == "no"


# --- asking for it ------------------------------------------------------------

def test_an_item_is_fetched_as_the_caller(monkeypatch):
    seen = {}

    class _Resp:
        status = 200

        def read(self):
            return json.dumps(_item(tracks=1)).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout=0):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        return _Resp()

    monkeypatch.setattr(item_mod.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr("agent_media_visual.reply._abs_url",
                        lambda: "http://abs.example")

    ok, out = item_mod.item_for_app("li_1", "tok-abc")
    assert ok is True
    assert "audioFiles" not in out["media"]
    assert seen["url"] == "http://abs.example/api/items/li_1?expanded=1&include=rssfeed"
    # The caller's own login, so ABS enforces its own library permissions.
    assert seen["auth"] == "Bearer tok-abc"


def test_no_bearer_is_a_401_before_abs_is_troubled(monkeypatch):
    monkeypatch.setattr("agent_media_visual.reply._abs_url",
                        lambda: "http://abs.example")
    ok, err = item_mod.item_for_app("li_1", "  ")
    assert (ok, err["status"]) == (False, 401)


def test_no_item_id_is_a_400():
    ok, err = item_mod.item_for_app("", "tok")
    assert (ok, err["status"]) == (False, 400)


def test_abs_rejecting_the_token_stays_a_401(monkeypatch):
    def fake_open(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)

    monkeypatch.setattr(item_mod.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr("agent_media_visual.reply._abs_url",
                        lambda: "http://abs.example")
    ok, err = item_mod.item_for_app("li_1", "tok")
    assert (ok, err["status"]) == (False, 401)


def test_abs_being_down_is_never_a_401(monkeypatch):
    # The app answers a 401 by refreshing its token, and a failed refresh logs
    # the user out. An outage must not end the session.
    def fake_open(req, timeout=0):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(item_mod.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr("agent_media_visual.reply._abs_url",
                        lambda: "http://abs.example")
    ok, err = item_mod.item_for_app("li_1", "tok")
    assert (ok, err["status"]) == (False, 503)


def test_an_answer_that_is_not_an_item_is_a_502(monkeypatch):
    class _Resp:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(item_mod.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp())
    monkeypatch.setattr("agent_media_visual.reply._abs_url",
                        lambda: "http://abs.example")
    ok, err = item_mod.item_for_app("li_1", "tok")
    assert (ok, err["status"]) == (False, 502)
