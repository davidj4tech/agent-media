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
    monkeypatch.delenv("MEDIA_SPEECH_RENDER_SASONICA", raising=False)
    assert not device_voice.renders_on_device("sasonica")
    monkeypatch.setenv("MEDIA_SPEECH_RENDER_SASONICA", "device")
    assert device_voice.renders_on_device("sasonica")
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
    monkeypatch.setenv("MEDIA_SPEECH_DEVICE_VOICE_SASONICA", "en-au-x-aua-local")
    monkeypatch.setenv("MEDIA_SPEECH_CLIP_BASEURL_SASONICA", "http://red5:8780/audio")
    uri = _clip_uri_for(str(clip), Target(name="sasonica"))
    assert uri.startswith("tts:20260927--claude--003?text=")
    head, _, voice = uri.partition("&voice=")
    assert unquote(head.split("text=", 1)[1]) == "It's done, and 100% green & tested."
    assert voice == "en-au-x-aua-local"


def test_a_tts_uri_passes_through_and_audio_clips_are_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_CLIP_BASEURL_SASONICA", "http://red5:8780/audio")
    assert _clip_uri_for("tts:x?text=a", Target(name="sasonica")) == "tts:x?text=a"
    assert (_clip_uri_for(str(tmp_path / "a--000.mp3"), Target(name="sasonica"))
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


def test_the_conversations_library_can_be_switched_off(monkeypatch):
    from agent_media_core import book_tracks, feed_debounce

    monkeypatch.setenv("MEDIA_FEED_BASE_URL", "http://red5:8782")
    monkeypatch.delenv("MEDIA_CONVERSATIONS_LIBRARY", raising=False)
    assert book_tracks.enabled()
    monkeypatch.setenv("MEDIA_CONVERSATIONS_LIBRARY", "0")
    assert not book_tracks.enabled()
    assert not feed_debounce.enabled(), "no publish is armed for a turn"
    assert not feed_debounce.arm()


def test_the_apps_choice_comes_before_the_env(monkeypatch):
    monkeypatch.setenv("MEDIA_SPEECH_RENDER_SASONICA", "device")
    monkeypatch.setenv("MEDIA_SPEECH_DEVICE_VOICE_SASONICA", "en-au-x-aua-network")
    assert device_voice.override_for("sasonica") is None
    device_voice.set_override("sasonica", "server")
    assert not device_voice.renders_on_device("sasonica")
    assert device_voice.voice_for("sasonica") == "en-au-x-aua-network", "no voice: the env's"
    device_voice.set_override("sasonica", "phone", "en-au-x-auc-network")
    assert device_voice.renders_on_device("sasonica")
    assert device_voice.voice_for("sasonica") == "en-au-x-auc-network"
    # Another target's choice is its own, and one without is the env's.
    monkeypatch.delenv("MEDIA_SPEECH_RENDER_SASONICA")
    device_voice.set_override("rooms", "phone")
    assert device_voice.renders_on_device("sasonica")
    assert device_voice.can_render("rooms") and not device_voice.can_render("local")
    assert device_voice.overrides() == {
        "sasonica": {"mode": "phone", "voice": "en-au-x-auc-network"},
        "rooms": {"mode": "phone", "voice": None}}


def test_the_server_voice_is_the_engines(monkeypatch):
    for k in ("MEDIA_RENDER_ENGINE", "CLAUDE_TTS_ENGINE", "MEDIA_RENDER_VOICE",
              "MEDIA_RENDER_VOICE_EDGE", "MEDIA_RENDER_VOICE_PIPER"):
        monkeypatch.delenv(k, raising=False)
    assert device_voice.server_voice() == "server default"
    monkeypatch.setenv("MEDIA_RENDER_VOICE_EDGE", "en-AU-NatashaNeural")
    assert device_voice.server_voice() == "en-AU-NatashaNeural"
    monkeypatch.setenv("MEDIA_RENDER_ENGINE", "piper")
    assert device_voice.server_voice() == "server default"


def test_a_microsoft_voiced_clip_names_a_google_voice_to_fall_back_to(tmp_path, monkeypatch):
    monkeypatch.delenv("MEDIA_SPEECH_DEVICE_FALLBACK_VOICE", raising=False)
    clip = tmp_path / "r--claude--000.tts"
    clip.write_text("Hello there.")
    uri = device_voice.tts_uri(clip, "edge:en-AU-NatashaNeural")
    assert "&voice=edge%3Aen-AU-NatashaNeural" in uri
    assert uri.endswith("&fallback=en-au-x-aua-network")
    assert "fallback" not in device_voice.tts_uri(clip, "en-au-x-aua-network")
    assert device_voice.find_voice("edge:en-AU-NatashaNeural")


def _raw(short, gender, locale_name):
    return {"ShortName": short, "Gender": gender, "Locale": short.rsplit("-", 1)[0],
            "LocaleName": locale_name,
            "FriendlyName": f"Microsoft X Online (Natural) - {locale_name}"}


def test_the_voices_are_grouped_by_language_then_accent(monkeypatch):
    raw = [_raw("fr-FR-DeniseNeural", "Female", "French (France)"),
           _raw("en-US-AvaMultilingualNeural", "Female", "English (United States)"),
           _raw("en-AU-WilliamMultilingualNeural", "Male", "English (Australia)"),
           _raw("en-AU-NatashaNeural", "Female", "English (Australia)"),
           _raw("de-DE-KatjaNeural", "Female", "German (Germany)"),
           _raw("en-GB-RyanNeural", "Male", "English (United Kingdom)")]
    monkeypatch.setattr(device_voice, "_fetch_edge_voices", lambda: raw)
    langs = device_voice.languages()
    # English first, then by name.
    assert [(lang["code"], lang["name"]) for lang in langs] == [
        ("en", "English"), ("fr", "French"), ("de", "German")]
    en = langs[0]["accents"]
    assert [a["name"] for a in en] == ["Australia", "United Kingdom", "United States"]
    au = [(v["label"], v["where"]) for v in en[0]["voices"]]
    # Microsoft's by name, then Google's, which only the phone has.
    assert au[:2] == [("Natasha", ["phone", "server"]), ("William", ["phone", "server"])]
    assert au[2:] == [(v["label"], ["phone"]) for v in device_voice.GOOGLE_VOICES]
    ava = device_voice.find_voice("edge:en-US-AvaMultilingualNeural")
    assert ava == {"name": "edge:en-US-AvaMultilingualNeural", "label": "Ava",
                   "gender": "Female", "locale": "en-US", "language": "English",
                   "accent": "United States", "where": ["phone", "server"]}


def test_the_list_is_kept_a_day_and_the_stale_one_serves_offline(monkeypatch):
    import json
    import time

    from agent_media_core._paths import state_dir

    calls = []

    def fetch():
        calls.append(1)
        return [_raw("fr-FR-DeniseNeural", "Female", "French (France)")]

    monkeypatch.setattr(device_voice, "_fetch_edge_voices", fetch)
    assert [v["label"] for v in device_voice.edge_voices()] == ["Denise"]
    device_voice.edge_voices()
    assert len(calls) == 1, "cached"
    cache = state_dir() / device_voice.EDGE_CACHE_NAME
    data = json.loads(cache.read_text())
    data["fetched"] = time.time() - device_voice.EDGE_CACHE_TTL_S - 1
    cache.write_text(json.dumps(data))
    monkeypatch.setattr(device_voice, "_last_fetch_try", data["fetched"])

    def offline():
        calls.append(1)
        raise OSError("down")

    monkeypatch.setattr(device_voice, "_fetch_edge_voices", offline)
    assert [v["label"] for v in device_voice.edge_voices()] == ["Denise"], "stale serves"
    assert len(calls) == 2
    device_voice.edge_voices()
    assert len(calls) == 2, "a failed fetch is not retried at once"
    # No cache and no Microsoft: the built-in twelve.
    cache.unlink()
    monkeypatch.setattr(device_voice, "_last_fetch_try", 0.0)
    names = [v["name"] for v in device_voice.edge_voices()]
    assert len(names) == 12 and names[0] == "edge:en-AU-NatashaNeural"


def test_the_server_renders_in_the_chosen_microsoft_voice(monkeypatch):
    from agent_media_core.intake import submit
    from agent_media_core.types import Event, Source

    monkeypatch.setenv("MEDIA_RENDER_VOICE_EDGE", "en-AU-NatashaNeural")
    monkeypatch.delenv("MEDIA_RENDER_ENGINE", raising=False)
    monkeypatch.delenv("CLAUDE_TTS_ENGINE", raising=False)
    sas = Target(name="sasonica")
    ev = Event(source=Source.CLAUDE_CODE, text="Hi.")
    assert submit._engine_and_voice(ev, sas) == ("edge", "en-AU-NatashaNeural")
    device_voice.set_override("sasonica", "server", "edge:en-GB-RyanNeural")
    assert submit._engine_and_voice(ev, sas) == ("edge", "en-GB-RyanNeural")
    assert device_voice.server_voice("sasonica") == "en-GB-RyanNeural"
    assert device_voice.server_voice() == "en-AU-NatashaNeural"
    # Another target, an event with its own voice, or a Google voice: as before.
    assert submit._engine_and_voice(ev, Target(name="rooms")) == ("edge", "en-AU-NatashaNeural")
    own = Event(source=Source.CLAUDE_CODE, text="Hi.", voice="en-US-AvaNeural")
    assert submit._engine_and_voice(own, sas) == ("edge", "en-US-AvaNeural")
    device_voice.set_override("sasonica", "server", "en-au-x-aua-network")
    assert submit._engine_and_voice(ev, sas) == ("edge", "en-AU-NatashaNeural")
    # On the phone, the phone's.
    device_voice.set_override("sasonica", "phone", "edge:en-GB-RyanNeural")
    assert device_voice.server_choice("sasonica") is None
