"""Per-conversation voice resolution and pinned device clips; no real audio."""
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_media_core import session_voice as V
from agent_media_core.intake import submit as S
from agent_media_core.render import device
from agent_media_core.types import Event, Source, Target

A, B = "session-a", "session-b"
MOLLY = "edge:en-NZ-MollyNeural"
RYAN = "edge:en-GB-RyanNeural"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith(("MEDIA_", "RELAY_")):
            monkeypatch.delenv(name, raising=False)
    for name in ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv("MEDIA_RENDER_ENGINE", "edge")
    monkeypatch.setenv("MEDIA_RENDER_VOICE", "en-AU-NatashaNeural")


def event(session=A, **kwargs):
    return Event("Words to speak", Source.CODEX, metadata={"session": session}, **kwargs)


def test_voice_is_persisted_per_conversation_and_default_is_live():
    target = Target("local")
    assert S._engine_and_voice(event(), target) == ("edge", "en-AU-NatashaNeural")
    V.set_voice(A, MOLLY)
    assert V.voice_for(A) == MOLLY and V.voice_for(B) is None
    assert S._engine_and_voice(event(), target) == ("edge", "en-NZ-MollyNeural")
    assert S._engine_and_voice(event(B), target) == ("edge", "en-AU-NatashaNeural")
    device.set_override("local", "server", RYAN)
    assert S._engine_and_voice(event(), target) == ("edge", "en-NZ-MollyNeural")
    V.set_voice(A, None)
    assert S._engine_and_voice(event(), target) == ("edge", "en-GB-RyanNeural")


def test_explicit_event_engine_or_voice_keeps_precedence():
    V.set_voice(A, MOLLY)
    assert S._engine_and_voice(event(voice="explicit"), Target("local")) == ("edge", "explicit")
    assert S._engine_and_voice(event(engine="openai", voice="marin"), Target("local")) == ("openai", "marin")


def test_device_choice_is_encoded_and_saved_with_the_clip(tmp_path):
    V.set_voice(A, MOLLY)
    device.set_override("sasonica", "phone", RYAN)
    engine, voice = S._engine_and_voice(event(), Target("sasonica"))
    assert (engine, voice) == ("device", MOLLY)
    clip = tmp_path / "thought.tts"
    assert device.write_clip("Thinking aloud", clip, pinned_voice=voice)[0]
    V.set_voice(A, None)
    device.set_override("sasonica", "phone", "edge:en-US-AvaMultilingualNeural")
    assert device.clip_voice(clip) == MOLLY
    assert "voice=edge%3Aen-NZ-MollyNeural" in device.tts_uri(clip, device.clip_voice(clip))


def test_simultaneous_changes_do_not_lose_other_conversations():
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: V.set_voice(str(n), MOLLY), range(20)))
    assert all(V.voice_for(str(n)) == MOLLY for n in range(20))


def test_bad_store_falls_back_without_breaking_speech():
    V._path().parent.mkdir(parents=True)
    V._path().write_text("not JSON")
    assert V.voice_for(A) is None


def test_conversation_choice_overrides_a_hooks_automatic_workspace_voice():
    V.set_voice(A, MOLLY)
    automatic = Event("A question", Source.CLAUDE_CODE, voice="en-AU-NatashaNeural",
                      metadata={"session": A, "voice_from": "session", "ask": True})
    assert S._engine_and_voice(automatic, Target("local")) == ("edge", "en-NZ-MollyNeural")
    device.set_override("sasonica", "phone", RYAN)
    assert S._engine_and_voice(automatic, Target("sasonica")) == ("device", MOLLY)


def test_generic_hook_engine_does_not_mask_a_conversation_voice(monkeypatch):
    from agent_media_core.intake import _hook_stdin

    V.set_voice(A, MOLLY)
    device.set_override("sasonica", "phone", RYAN)
    monkeypatch.setenv("MEDIA_RENDER_ENGINE", "edge")
    monkeypatch.setenv("CODEX_TTS_ENABLED", "1")
    monkeypatch.delenv("CODEX_TTS_ENGINE", raising=False)
    monkeypatch.delenv("CODEX_TTS_VOICE", raising=False)
    monkeypatch.setattr(_hook_stdin, "_load_env_file", lambda _: None)
    submitted = []
    monkeypatch.setattr(_hook_stdin, "submit_event", submitted.append)

    assert _hook_stdin.run(Source.CODEX, "CODEX", text="A reply",
                           metadata={"session": A}) == 0
    assert len(submitted) == 1
    assert submitted[0].engine is None
    assert S._engine_and_voice(submitted[0], Target("sasonica")) == ("device", MOLLY)


def test_explicit_hook_engine_still_wins_over_conversation_voice(monkeypatch):
    from agent_media_core.intake import _hook_stdin

    V.set_voice(A, MOLLY)
    monkeypatch.setenv("CODEX_TTS_ENABLED", "1")
    monkeypatch.setenv("CODEX_TTS_ENGINE", "kokoro")
    monkeypatch.setattr(_hook_stdin, "_load_env_file", lambda _: None)
    submitted = []
    monkeypatch.setattr(_hook_stdin, "submit_event", submitted.append)

    assert _hook_stdin.run(Source.CODEX, "CODEX", text="A reply",
                           metadata={"session": A}) == 0
    assert submitted[0].engine == "kokoro"
    assert S._engine_and_voice(submitted[0], Target("sasonica")) == ("kokoro", None)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("mode", ["phone", "server"])
def test_submission_uses_the_voice_on_both_render_paths(monkeypatch, streaming, mode):
    from pathlib import Path
    from agent_media_core.state import StateStore
    from agent_media_core.types import Priority

    V.set_voice(A, MOLLY)
    device.set_override("local", mode, RYAN)
    monkeypatch.setenv("MEDIA_EARCONS", "0")
    monkeypatch.setenv("MEDIA_FOLLOW_AUTO", "0")
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setattr(S, "_source_place", lambda *a: ("", ""))
    monkeypatch.setattr(S, "_gate_hold", lambda *a: False)
    monkeypatch.setattr(S, "_ringer_hold", lambda *a: None)
    monkeypatch.setattr(S, "_busy_hold", lambda *a: None)
    monkeypatch.setattr(S, "_clip_duration", lambda *a: 0)
    monkeypatch.setattr(S, "_remote_playlist", lambda *a: False)
    rendered, played = [], []
    def render(text, outfile, **kwargs):
        rendered.append((kwargs["engine"], kwargs["voice"]))
        Path(outfile).write_bytes(b"fake audio")
        return True, ""
    monkeypatch.setattr(S, "render_text", render)

    class Sink:
        def play(self, uri, target, **kw):
            played.append((Path(uri).suffix, device.clip_voice(uri)))
        def idle(self, target): return True
        def paused(self, target): return False
        def stop(self, target): pass

    class Coord:
        def pre_pause_remote(self): pass
        def cancel_pre_pause(self): pass
        def before_speech(self, **kw): pass
        def after_speech(self): pass
        def speaking_line(self, text): pass

    ev = Event("Words in the chosen voice.", Source.CODEX, priority=Priority.LOW,
               target=Target("local"), metadata={"session": A, "thinking": True})
    kwargs = dict(state=StateStore(), sink=Sink(), coordinator=Coord())
    if streaming:
        S.submit_stream(iter([ev.text]), ev, **kwargs)
    else:
        S.submit_event(ev, **kwargs)
    if mode == "phone":
        assert played == [(".tts", MOLLY)]
        assert rendered == []
    else:
        assert rendered == [("edge", "en-NZ-MollyNeural")]
        assert played == [(".mp3", None)]
