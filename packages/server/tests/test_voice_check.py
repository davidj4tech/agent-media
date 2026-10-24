"""`POST /voice/check` (voice_check.py): hands-free asks whether the heard
words were what was meant before they send (David, 24 Oct 2026: "an
interpretation of what you think I said, and ask me to confirm").

The model is never called: `_summary._chat` is replaced by a stand-in, and
nothing is spoken: `voice_check.speak` is replaced by a recorder.
"""

from __future__ import annotations

import os
import time

import pytest

from agent_media_core.intake import _summary
from agent_media_server import voice_check
from test_contract import AUTH, call, server, signed_in, typed  # noqa: F401 — fixtures

S1 = "0a1b2c3d-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("MEDIA_", "RELAY_")):
            monkeypatch.delenv(k, raising=False)
    for d in ("HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(d, str(tmp_path / d.lower()))


@pytest.fixture
def model(monkeypatch):
    """What the model answers (`answer`), and what it was asked (`asked`)."""
    state = {"answer": '{"sure": true, "gist": "restart the music server"}', "asked": [],
             "delay": 0.0}

    def chat(system, user, timeout, model=None):
        state["asked"].append({"user": user, "timeout": timeout, "model": model})
        if state["delay"]:
            time.sleep(state["delay"])
        a = state["answer"]
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(_summary, "_chat", chat)
    return state


@pytest.fixture
def spoken(monkeypatch):
    said: list = []
    monkeypatch.setattr(voice_check, "speak", lambda session, text: said.append((session, text)))
    return said


def test_sure_words_send_as_before(model):
    got = voice_check.check("restart the music server", "Anything else?", 0.92)
    assert got == {"sure": True, "gist": "restart the music server"}
    assert "<heard>restart the music server</heard>" in model["asked"][0]["user"]
    assert "Anything else?" in model["asked"][0]["user"]


def test_the_model_unsure_asks(model):
    model["answer"] = '```json\n{"sure": false, "gist": "Restart the music server?"}\n```'
    got = voice_check.check("rest are the music sever")
    assert got == {"sure": False, "gist": "Restart the music server?"}


def test_low_confidence_alone_makes_it_unsure(model, monkeypatch):
    got = voice_check.check("restart the music server", confidence=0.3)
    assert got["sure"] is False and got["why"] == "low_confidence"
    assert got["gist"] == "restart the music server"
    # Unknown confidence (Android gives 0 or none) does not count as low.
    assert voice_check.check("restart the music server", confidence=0.0)["sure"] is True
    assert voice_check.check("restart the music server", confidence=-1)["sure"] is True
    monkeypatch.setenv("MEDIA_VOICE_CHECK_MIN_CONFIDENCE", "0.2")
    assert voice_check.check("restart the music server", confidence=0.3)["sure"] is True


@pytest.mark.parametrize("answer", [None, "", "I think they meant…", '{"gist": "x"}',
                                    '{"sure": "no", "gist": "x"}', RuntimeError("boom"),
                                    '{"sure": false, "gist": ""}'])
def test_any_failure_is_sure(model, answer):
    model["answer"] = answer
    got = voice_check.check("rest are the music sever", confidence=0.1)
    assert got["sure"] is True and got["gist"] == ""


def test_a_slow_model_is_not_waited_for(model, monkeypatch):
    monkeypatch.setenv("MEDIA_VOICE_CHECK_TIMEOUT", "0.2")
    model["answer"] = '{"sure": false, "gist": "late"}'
    model["delay"] = 1.0
    t0 = time.monotonic()
    got = voice_check.check("rest are the music sever")
    assert time.monotonic() - t0 < 0.8
    assert got == {"sure": True, "gist": "", "why": "no_answer"}


def test_switched_off_or_no_words_never_asks(model, monkeypatch):
    assert voice_check.check("   ")["why"] == "no_words"
    monkeypatch.setenv("MEDIA_VOICE_CHECK", "0")
    assert voice_check.check("rest are the music sever") == {"sure": True, "gist": "", "why": "off"}
    assert not model["asked"]


def test_the_model_is_the_configured_fast_one(model, monkeypatch):
    monkeypatch.setenv("MEDIA_FOLLOWUP_MODEL", "fast-one")
    voice_check.check("hello")
    monkeypatch.setenv("MEDIA_VOICE_CHECK_MODEL", "check-one")
    voice_check.check("hello")
    assert [a["model"] for a in model["asked"]] == ["fast-one", "check-one"]


def test_a_long_gist_is_cut_to_a_sentence(model):
    model["answer"] = '{"sure": false, "gist": "%s"}' % ("word " * 200)
    assert len(voice_check.check("x")["gist"]) <= voice_check.MAX_GIST + 1


def test_route_unsure_speaks_the_question_in_the_thread(server, signed_in, model, spoken):
    model["answer"] = '{"sure": false, "gist": "restart the music server"}'
    res, body = call(server, "POST", "/voice/check",
                     {"text": "rest are the music sever", "session": S1, "confidence": 0.8,
                      "context": "Anything else?", "say": "Did you mean: {gist}?"}, AUTH)
    assert res.status == 200, body
    assert body == {"ok": True, "sure": False, "gist": "restart the music server", "spoken": True}
    assert spoken == [(S1, "Did you mean: restart the music server?")]


def test_route_sure_or_without_say_speaks_nothing(server, signed_in, model, spoken):
    res, body = call(server, "POST", "/voice/check", {"text": "restart it", "session": S1,
                                                      "say": "Did you mean: {gist}?"}, AUTH)
    assert res.status == 200 and body["sure"] is True and body["spoken"] is False
    model["answer"] = '{"sure": false, "gist": "restart it"}'
    res, body = call(server, "POST", "/voice/check", {"text": "restart it", "session": S1}, AUTH)
    assert body["sure"] is False and body["spoken"] is False
    assert not spoken


def test_route_needs_a_signed_in_caller(server, model, spoken):
    res, _ = call(server, "POST", "/voice/check", {"text": "hello", "session": S1,
                                                   "say": "{gist}?"}, {})
    assert res.status in (401, 403)
    assert not model["asked"] and not spoken


def test_route_refuses(server, signed_in, model, spoken):
    res, _ = call(server, "POST", "/voice/check", {"session": S1}, AUTH)
    assert res.status == 400
    res, _ = call(server, "POST", "/voice/check", {"text": "x", "session": "../x"}, AUTH)
    assert res.status == 400
    assert not model["asked"] and not spoken


def test_speak_submits_privately_in_the_threads_voice(monkeypatch):
    from agent_media_core.intake import submit

    got = []
    monkeypatch.setattr(submit, "submit_event", lambda event, state=None: got.append(event))
    voice_check.speak(S1, "Did you mean: yes?")
    for _ in range(50):
        if got:
            break
        time.sleep(0.02)
    assert got and got[0].text == "Did you mean: yes?"
    md = got[0].metadata
    assert md["session"] == S1 and md["private"] is True and md["voice_from"] == "session"
