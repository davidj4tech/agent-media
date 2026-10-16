"""`GET /speech/clip` (16 Oct 2026): one sentence as mp3, for the app in a
browser speaking replies itself — the words and voice of a `tts:` load op.

Nothing renders for real: `render_text` is a recorder that writes bytes, and
the cache is the test's own (XDG_CACHE_HOME).
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from agent_media_core import render

from test_contract import AUTH, call, server, signed_in, typed  # noqa: F401


@pytest.fixture()
def renders(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("MEDIA_RENDER_ENGINE", raising=False)
    monkeypatch.delenv("CLAUDE_TTS_ENGINE", raising=False)
    monkeypatch.setenv("MEDIA_RENDER_VOICE_EDGE", "en-GB-RyanNeural")
    ran: list = []

    def fake(text, outfile, *, engine, voice=None, **_kw):
        ran.append((text, engine, voice))
        outfile.write_bytes(b"ID3fake:" + text.encode())
        return True, ""

    monkeypatch.setattr(render, "render_text", fake)
    return ran


def _get(server, text, voice="", headers=AUTH):
    return call(server, "GET", f"/speech/clip?text={quote(text)}&voice={quote(voice)}", headers=headers)


def test_a_sentence_comes_back_as_mp3(server, signed_in, renders):
    res, body = _get(server, "Hello there.", "edge:en-AU-NatashaNeural")
    assert res.status == 200
    assert res.getheader("Content-Type") == "audio/mpeg"
    assert body == b"ID3fake:Hello there."
    assert renders == [("Hello there.", "edge", "en-AU-NatashaNeural")]


def test_the_same_sentence_is_rendered_once(server, signed_in, renders):
    _get(server, "Again.", "edge:en-AU-NatashaNeural")
    res, body = _get(server, "Again.", "edge:en-AU-NatashaNeural")
    assert res.status == 200 and body == b"ID3fake:Again."
    assert len(renders) == 1


def test_a_phone_voice_gets_the_servers(server, signed_in, renders):
    res, _ = _get(server, "Hi.", "en-au-x-aua-network")
    assert res.status == 200
    assert renders == [("Hi.", "edge", "en-GB-RyanNeural")]


def test_no_words_no_clip(server, signed_in, renders):
    res, obj = _get(server, "  ")
    assert res.status == 400 and obj["ok"] is False
    assert renders == []


def test_nobody_gets_nothing(server, renders):
    res, obj = _get(server, "Hi.", headers={})
    assert res.status in (401, 403) and obj["ok"] is False
    assert renders == []


def test_a_failed_render_is_a_502(server, signed_in, renders, monkeypatch):
    monkeypatch.setattr(render, "render_text", lambda *a, **k: (False, "edge 503"))
    res, obj = _get(server, "Hi.")
    assert res.status == 502 and "edge 503" in obj["error"]


def test_old_clips_are_cleared_on_the_next_render(server, signed_in, renders, tmp_path):
    import os
    import time
    folder = tmp_path / "cache" / "agent-media" / "web-clips"
    folder.mkdir(parents=True)
    old = folder / "old.mp3"
    old.write_bytes(b"x")
    week = time.time() - 7 * 86400
    os.utime(old, (week, week))
    _get(server, "Fresh.")
    assert not old.exists()
