"""The listener's own words are a history row, not an mp3 (David, 18 Oct 2026).

A typed message is recorded with no render; the transcript, the log and a
thread's `pending` read the row; a replay that wants the question before its
answer reads it live — sentence text (`.tts` clips) to a target that renders on
the device, a render made at replay time for one that does not. Fakes only:
the state dir is the test's own, no sink or player is ever built, and nothing
reaches a device (render_text is faked wherever a render could happen).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_media_core import book_tracks, cli, session_feed
from agent_media_core.render import device as device_voice
from agent_media_core.state import StateStore

S = "6c73498c-02c1-4846-8350-a82006973571"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("MEDIA_", "RELAY_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    import agent_media_core.render.engines as eng

    def no_render(*a, **k):
        raise AssertionError("nothing renders in this test unless it says so")

    monkeypatch.setattr(eng, "render_text", no_render)
    return tmp_path


def _reply(st, at, text="The answer. It has two sentences."):
    return st.add_history(
        sink="speech", uri="/nowhere/reply.mp3", started_at=at, ended_at=at + 3,
        target="phone", source="claude", text=text,
        extras={"source_session": S, "clip_uris": ["/nowhere/reply.mp3"],
                "clip_durations_s": [3.0], "clip_sentences": [text]})


def _record(text="Can you check the build?"):
    assert book_tracks.record_listener_turn(S, text) is True
    rows = [r for r in StateStore().recent_history(sink="speech", limit=10)
            if r.get("source") == "listener"]
    assert len(rows) == 1
    return rows[0]


def test_the_turn_is_a_row_with_no_audio(isolated):
    row = _record()
    assert row["uri"] == "" and row["text"] == "You: Can you check the build?"
    assert not cli._row_has_audio(row)
    assert not list(isolated.rglob("*.mp3"))


def test_the_clipless_turn_is_in_the_conversation(isolated, monkeypatch):
    row = _record()
    ts = session_feed.turns(S)
    assert [(t.listener, t.text, t.clips) for t in ts] == [
        (True, "You: Can you check the build?", [])]
    monkeypatch.setattr(book_tracks, "_live_turn", lambda s: None)
    lines = book_tracks.conversation_log(S, isolated / "nofolder", positions=False)
    assert [(l["who"], l["text"], l.get("id")) for l in lines] == [
        ("you", "Can you check the build?", row["id"])]
    # The thread's `pending` rule: the last line is the listener's.
    assert lines[-1]["who"] == "you"


def test_audio_consumers_skip_the_clipless_turn(isolated, monkeypatch):
    """The shelf (off here) has nothing to place for it, and it must not stop
    the turns after it from being placed."""
    _record()
    ts = session_feed.turns(S)
    assert session_feed.build(ts, isolated / "out.mp3") is None
    folder = isolated / "shelf" / "item"
    monkeypatch.setattr(book_tracks, "folder_for", lambda *a, **k: folder)
    monkeypatch.setattr(book_tracks, "_read_manifest", lambda s: {})
    written = {}
    monkeypatch.setattr(book_tracks, "_write_manifest",
                        lambda s, m: written.update(m))
    assert book_tracks.export_session(S) == (folder, 0)
    assert written["turns"] == []


def test_replay_reads_the_question_live(isolated):
    st = StateStore()
    q = _record("Can you check the build on the server this morning? "
                "Then push it to the main branch when it is green.")
    rid = _reply(st, q["started_at"] + 5)
    reply = st.history_row(rid)
    question = cli._question_before(reply)
    assert question is not None and question["id"] == q["id"]
    playable = cli._question_to_play(question)
    ex = playable["extras"]
    assert ex["clip_sentences"] == [
        "Can you check the build on the server this morning?",
        "Then push it to the main branch when it is green."]
    assert all(device_voice.is_clip(u) for u in ex["clip_uris"])
    assert playable["uri"] == ex["clip_uris"][0]
    assert len(ex["clip_durations_s"]) == 2 and all(d > 0 for d in ex["clip_durations_s"])
    assert [Path(u).read_text() for u in ex["clip_uris"]] == ex["clip_sentences"]
    # In the listener's voice, not the reply's.
    assert device_voice.clip_voice(ex["clip_uris"][0]) == f"edge:{book_tracks.LISTENER_VOICE}"
    # Beside the replies' clips, so /audio/ and a prefetch find it.
    from agent_media_core._paths import speech_audio_dir
    assert Path(ex["clip_uris"][0]).parent == speech_audio_dir()
    # The stored row is untouched.
    assert "clip_uris" not in (st.history_row(q["id"])["extras"] or {})


def test_a_device_target_is_handed_the_words(isolated, monkeypatch):
    """MEDIA_SPEECH_RENDER_<TARGET>=device: the question goes out as a reply's
    sentences do, a tts: address, in the listener's voice."""
    from agent_media_core.sinks import speech as sink
    from agent_media_core.types import Target

    st = StateStore()
    q = _record("Is it done?")
    st.history_row(_reply(st, q["started_at"] + 5))
    playable = cli._question_to_play(q)
    monkeypatch.setenv("MEDIA_SPEECH_RENDER_PHONE", "device")
    target = Target("phone")
    assert device_voice.renders_on_device("phone")
    uri = sink._clip_uri_for(playable["extras"]["clip_uris"][0], target)
    assert uri.startswith("tts:question-")
    assert "text=Is%20it%20done%3F" in uri
    assert f"voice=edge%3A{book_tracks.LISTENER_VOICE}" in uri


def test_a_server_target_renders_it_at_replay_time(isolated, monkeypatch):
    """A target that cannot render words has the question rendered here as
    the replay starts — the reply path's render_device_clips, once, in the
    listener's voice."""
    from agent_media_core.intake import submit

    q = _record("Is it done?")
    playable = cli._question_to_play(q)
    calls = []

    def fake(text, out, **k):
        calls.append((text, k.get("engine"), k.get("voice")))
        Path(out).write_bytes(b"ID3")
        return True, ""

    monkeypatch.setattr(submit, "render_text", fake)
    monkeypatch.setattr(submit, "_clip_duration", lambda p: 1.5)
    uris, durs = submit.render_device_clips(playable["extras"]["clip_uris"])
    assert calls == [("Is it done?", "edge", book_tracks.LISTENER_VOICE)]
    assert [Path(u).suffix for u in uris] == [".mp3"] and durs == [1.5]
    # Rendered once: a second replay reuses it.
    submit.render_device_clips(playable["extras"]["clip_uris"])
    assert len(calls) == 1


def test_an_old_row_keeps_its_clip(isolated):
    clip = isolated / "old-listener.mp3"
    clip.write_bytes(b"ID3")
    row = {"id": 9, "text": "You: hi", "source": "listener",
           "uri": str(clip), "extras": {"listener": True, "source_session": S,
                                        "clip_uris": [str(clip)],
                                        "clip_durations_s": [1.0]}}
    assert cli._question_to_play(row) is row
    # Swept: read live instead.
    clip.unlink()
    live = cli._question_to_play(row)
    assert live is not row and device_voice.is_clip(live["uri"])


def test_question_then_reply_and_reply_alone_when_it_cannot(isolated, monkeypatch):
    st = StateStore()
    q = _record("Is it done?")
    rid = _reply(st, q["started_at"] + 5)
    played = []
    results = iter([0])

    def fake_replay(row, **kw):
        played.append((row["id"], kw.get("then_id")))
        return next(results, 0)

    monkeypatch.setattr(cli, "_replay_row", fake_replay)
    monkeypatch.setattr(cli, "_resume_point", lambda row, question: None)
    monkeypatch.setattr(cli, "_speech_history",
                        lambda n, session=None, include_live=False: [st.history_row(rid)])
    assert cli._do_replay(1) == 0
    assert played == [(q["id"], rid)]

    # The question will not play (say the render failed): the reply alone.
    played.clear()
    results = iter([1, 0])
    assert cli._do_replay(1) == 0
    assert played == [(q["id"], rid), (rid, None)]

    # Nothing to voice at all: straight to the reply.
    played.clear()
    monkeypatch.setattr(cli, "_question_to_play", lambda q: None)
    assert cli._do_replay(1) == 0
    assert played == [(rid, None)]
