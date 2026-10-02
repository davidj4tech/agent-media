"""jev.py: the rule answers whenever Jev can't, and every question is logged."""

from __future__ import annotations

import json
import time
import urllib.error

import pytest

from agent_media_core import jev


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.delenv("MEDIA_JEV_MODE", raising=False)
    monkeypatch.delenv("MEDIA_JEV_THRESHOLD", raising=False)
    jev._event_cache.clear()


def _answering(monkeypatch, answer, ms=300):
    calls = []

    def fake(state, questions, timeout=None):
        calls.append((state, questions))
        (qid,) = questions
        return {qid: answer}, "ok", ms
    monkeypatch.setattr(jev, "ask", fake)
    return calls


def _log():
    return [json.loads(l) for l in jev.log_path().read_text().splitlines()]


def test_shadow_is_the_default_and_keeps_the_rule(monkeypatch):
    _answering(monkeypatch, {"noul": 0.97})
    d = jev.breaks_through({"text": "prod is down"}, False)
    assert (d.answer, d.source, d.jev, d.why) == (False, "rule", True, "shadow")
    assert d.p == pytest.approx(0.97)
    row = _log()[-1]
    assert row["question"] == "breaks_through"
    assert (row["rule"], row["jev"], row["source"]) == (False, True, "rule")
    assert row["state"]["item"]["text"] == "prod is down"


def test_on_and_sure_uses_jev(monkeypatch):
    monkeypatch.setenv("MEDIA_JEV_MODE", "on")
    _answering(monkeypatch, {"noul": 0.9})
    d = jev.breaks_through({"text": "deploy failed"}, False)
    assert (d.answer, d.source, d.why) == (True, "jev", "sure")


def test_on_but_unsure_keeps_the_rule(monkeypatch):
    monkeypatch.setenv("MEDIA_JEV_MODE", "on")
    _answering(monkeypatch, {"noul": 0.4})  # p = 0.6 for "no"
    d = jev.breaks_through({"text": "disk at 81%"}, True)
    assert (d.answer, d.source, d.why) == (True, "rule", "unsure")


def test_choice_reads_the_chosen_probability(monkeypatch):
    monkeypatch.setenv("MEDIA_JEV_MODE", "on")
    _answering(monkeypatch, {"choice": "light", "confidence": 0.3,
                             "probabilities": {"hold": 0.1, "light": 0.85,
                                               "free": 0.05}})
    d = jev.event_busy({"title": "Gym", "minutes": 60}, "hold")
    assert (d.answer, d.source) == ("light", "jev")
    assert d.p == pytest.approx(0.85)


def test_an_option_jev_was_not_offered_is_an_error(monkeypatch):
    monkeypatch.setenv("MEDIA_JEV_MODE", "on")
    _answering(monkeypatch, {"choice": "maybe", "probabilities": {"maybe": 1}})
    d = jev.catchup_worth([{"text": "done"}], "speak")
    assert (d.answer, d.source, d.why) == ("speak", "rule", "error")


def test_off_never_asks_or_logs(monkeypatch):
    monkeypatch.setenv("MEDIA_JEV_MODE", "off")
    calls = _answering(monkeypatch, {"noul": 1.0})
    d = jev.breaks_through({"text": "x"}, True)
    assert (d.answer, d.why) == (True, "off")
    assert calls == [] and not jev.log_path().exists()


def test_no_key_is_the_rule(monkeypatch):
    monkeypatch.setenv("MEDIA_JEV_MODE", "on")
    monkeypatch.delenv("TYPESAFE_API_KEY")
    d = jev.event_busy({"title": "Lunch"}, "hold")
    assert (d.answer, d.source, d.why) == ("hold", "rule", "nokey")


def test_the_key_comes_from_secrets_env(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    jev.secrets_path().parent.mkdir(parents=True)
    jev.secrets_path().write_text("# x\nHA_TOKEN=a\nTYPESAFE_API_KEY='k-1'\n")
    assert jev._key() == "k-1"


@pytest.mark.parametrize("exc,why", [
    (TimeoutError(), "timeout"),
    (urllib.error.URLError(TimeoutError()), "timeout"),
    (urllib.error.URLError("refused"), "error"),
    (urllib.error.HTTPError("u", 500, "boom", {}, None), "error"),
])
def test_network_failures_are_the_rule(monkeypatch, exc, why):
    monkeypatch.setenv("MEDIA_JEV_MODE", "on")

    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(jev.urllib.request, "urlopen", boom)
    d = jev.breaks_through({"text": "x"}, False)
    assert (d.answer, d.source, d.why) == (False, "rule", why)
    assert _log()[-1]["why"] == why


def test_the_request_is_systemone_shaped(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"answers": {"breaks_through": {
                "type": "noul", "noul": 0.2}}}).encode()

    def fake(req, timeout):
        seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
        seen["body"], seen["timeout"] = json.loads(req.data), timeout
        return Resp()
    monkeypatch.setattr(jev.urllib.request, "urlopen", fake)
    d = jev.breaks_through({"text": "y" * 1000, "thread": "radio"}, False)
    assert d.jev is False
    assert seen["url"] == jev.API_URL and seen["auth"] == "Bearer test-key"
    assert seen["timeout"] == jev.DEFAULT_TIMEOUT_S
    body = seen["body"]
    assert body["model"] == "jev-latest"
    assert body["questions"]["breaks_through"]["type"] == "noul"
    assert len(body["state"]["item"]["text"]) == jev.TEXT_CAP


def test_an_event_is_asked_once(monkeypatch):
    calls = _answering(monkeypatch, {"choice": "hold",
                                     "probabilities": {"hold": 0.9}})
    jev.event_busy({"id": "e1", "title": "1:1"}, "hold")
    jev.event_busy({"id": "e1", "title": "1:1"}, "hold")
    assert len(calls) == 1


def test_a_failed_event_is_asked_again(monkeypatch):
    monkeypatch.setattr(jev, "ask", lambda *a, **k: (None, "timeout", 1500))
    jev.event_busy({"id": "e2", "title": "1:1"}, "hold")
    assert "e2" not in jev._event_cache


def test_facts(monkeypatch):
    now = time.time()
    path = jev.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"ts": now - 90000, "jev": True, "rule": False, "ms": 9, "source": "rule"},
        {"ts": now - 60, "jev": True, "rule": False, "ms": 400, "source": "rule"},
        {"ts": now - 50, "jev": "hold", "rule": "hold", "ms": 600, "source": "rule"},
        {"ts": now - 40, "jev": None, "rule": "hold", "ms": 1500,
         "source": "rule", "why": "timeout"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    f = jev.facts(now)
    assert f["jev"] == "down" and f["jev_mode"] == "shadow"
    assert f["jev_asked_24h"] == "2"
    assert f["jev_p50_ms"] == "500"
    assert f["jev_disagree_24h"] == "1"
    assert "jev_overrides_24h" not in f
    monkeypatch.delenv("TYPESAFE_API_KEY")
    assert jev.facts(now)["jev"] == "nokey"
    path.unlink()
    assert jev.facts(now) == {}
