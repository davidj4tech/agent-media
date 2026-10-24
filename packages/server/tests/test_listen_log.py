"""`POST /device/listen-log` (listen_log.py, §6.21c): the phone's mic listens,
reported outbound so a miss can be read from the server (David, 25 Oct 2026:
"so often the mic doesn't seem to accept my spoken text")."""

from __future__ import annotations

import json
import os

import pytest

from agent_media_server import listen_log
from test_contract import call, server, typed  # noqa: F401 — fixtures
from test_mic import _device


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("MEDIA_", "RELAY_")):
            monkeypatch.delenv(k, raising=False)
    for d in ("HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(d, str(tmp_path / d.lower()))
    monkeypatch.setenv("AMUX_AUTH_TOKEN", "hosttok")


@pytest.fixture()
def pixel():
    return _device("Pixel 8a")[0]


def _lines():
    p = listen_log._path()
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_a_batch_is_appended_with_the_device(server, pixel):
    batch = {"lines": [
        {"t": 1.5, "at": "handsfree", "outcome": "echo", "text": "yes the short one", "voiceNear": True,
         "session": "s1", "junk": {"x": 1}},
        {"t": 2.0, "at": "handsfree", "outcome": "rescued", "text": "turn it down", "error": "error_11"},
        {"t": 3.0, "at": "countdown"},                      # no outcome: dropped
        "not a line",
    ]}
    res, body = call(server, "POST", "/device/listen-log", batch, pixel)
    assert res.status == 200 and body["kept"] == 2, body
    got = _lines()
    assert [g["outcome"] for g in got] == ["echo", "rescued"]
    assert got[0]["device"] == "Pixel 8a" and got[0]["voiceNear"] is True and "junk" not in got[0]
    assert got[1]["error"] == "error_11" and got[1]["received"] > 0


def test_a_paired_device_only(server):
    lines = {"lines": [{"outcome": "taken"}]}
    assert call(server, "POST", "/device/listen-log", lines, {})[0].status == 401
    host = {"Authorization": "Bearer hosttok"}
    assert call(server, "POST", "/device/listen-log", lines, host)[0].status in (401, 403)
    assert not _lines()


def test_lines_must_be_a_list(server, pixel):
    assert call(server, "POST", "/device/listen-log", {"lines": "x"}, pixel)[0].status == 400


def test_long_text_is_cut_and_the_file_is_capped(server, pixel, monkeypatch):
    monkeypatch.setattr(listen_log, "KEEP_LINES", 10)
    call(server, "POST", "/device/listen-log", {"lines": [{"outcome": "taken", "text": "w" * 1000}]}, pixel)
    assert len(_lines()[0]["text"]) == listen_log.MAX_CHARS
    for i in range(5):
        call(server, "POST", "/device/listen-log", {"lines": [{"outcome": "empty", "t": i + j / 10} for j in range(5)]}, pixel)
    got = _lines()
    assert len(got) <= 12 and got[-1]["t"] == 4.4
