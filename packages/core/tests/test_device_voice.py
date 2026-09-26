"""The device's own voice: a target that renders words gets `tts:` clips.

A reply to such a target is split and filed like any other, but each clip is a
`.tts` file holding its sentence, and the player is handed the words. Replay
elsewhere renders the audio then (clips on demand); the ABS export skips it.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from agent_media_core.render import device as device_voice
from agent_media_core.sinks.speech import _clip_uri_for
from agent_media_core.types import Target


def test_only_a_target_set_to_device_renders_on_it(monkeypatch):
    monkeypatch.delenv("MEDIA_SPEECH_RENDER_NEXT", raising=False)
    assert not device_voice.renders_on_device("next")
    monkeypatch.setenv("MEDIA_SPEECH_RENDER_NEXT", "device")
    assert device_voice.renders_on_device("next")
    assert not device_voice.renders_on_device("rooms")


def test_the_clip_is_the_sentence_and_its_length_an_estimate(tmp_path):
    clip = tmp_path / "r--claude--000.tts"
    ok, err = device_voice.write_clip("It's done.", clip, engine="x", voice=None)
    assert ok and err is None
    assert clip.read_text() == "It's done."
    assert device_voice.is_clip(clip)
    # 10 characters at 15/s, plus the lead in.
    assert abs(device_voice.estimate_duration(clip) - (0.2 + 10 / 15)) < 1e-9


def test_the_player_is_handed_the_words(tmp_path, monkeypatch):
    clip = tmp_path / "20260927--claude--003.tts"
    clip.write_text("It's done, and 100% green & tested.")
    monkeypatch.setenv("MEDIA_SPEECH_DEVICE_VOICE_NEXT", "en-au-x-aua-local")
    monkeypatch.setenv("MEDIA_SPEECH_CLIP_BASEURL_NEXT", "http://red5:8780/audio")
    uri = _clip_uri_for(str(clip), Target(name="next"))
    assert uri.startswith("tts:20260927--claude--003?text=")
    head, _, voice = uri.partition("&voice=")
    assert unquote(head.split("text=", 1)[1]) == "It's done, and 100% green & tested."
    assert voice == "en-au-x-aua-local"


def test_a_tts_uri_passes_through_and_audio_clips_are_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_CLIP_BASEURL_NEXT", "http://red5:8780/audio")
    assert _clip_uri_for("tts:x?text=a", Target(name="next")) == "tts:x?text=a"
    assert (_clip_uri_for(str(tmp_path / "a--000.mp3"), Target(name="next"))
            == "http://red5:8780/audio/a--000.mp3")


def test_replay_elsewhere_renders_once_and_keeps_it(tmp_path, monkeypatch):
    from agent_media_core.intake import submit

    calls = []

    def fake_render(text, outfile, *, engine, voice=None, **_):
        calls.append((text, Path(outfile).name, engine))
        Path(outfile).write_bytes(b"ID3fake")
        return True, ""

    monkeypatch.setattr(submit, "render_text", fake_render)
    monkeypatch.setattr(submit, "_default_engine", lambda: "edge")
    monkeypatch.setattr(submit, "_clip_duration",
                        lambda p: 1.5 if str(p).endswith(".mp3") else 0.0)
    a = tmp_path / "r--claude--000.tts"
    a.write_text("First.")
    b = tmp_path / "r--claude--001.tts"
    b.write_text("Second.")

    uris, durs = submit.render_device_clips([str(a), str(b)])
    assert uris == [str(a.with_suffix(".mp3")), str(b.with_suffix(".mp3"))]
    assert durs == [1.5, 1.5]
    assert [c[0] for c in calls] == ["First.", "Second."]

    submit.render_device_clips([str(a), str(b)])
    assert len(calls) == 2, "a clip rendered once is kept"


def test_a_clip_that_will_not_render_is_left_out(tmp_path, monkeypatch):
    from agent_media_core.intake import submit

    monkeypatch.setattr(submit, "render_text", lambda *a, **k: (False, "503"))
    monkeypatch.setattr(submit, "_default_engine", lambda: "edge")
    a = tmp_path / "r--claude--000.tts"
    a.write_text("First.")
    assert submit.render_device_clips([str(a)]) == ([], [])
