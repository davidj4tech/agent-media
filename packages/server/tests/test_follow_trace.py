"""`POST /follow/trace` (follow_trace.py): the app's bold, logged beside the row."""

from __future__ import annotations

import json

from agent_media_server import follow_trace
from test_contract import AUTH, call, server, signed_in, typed  # noqa: F401 — fixtures


def test_a_batch_is_written_with_the_row(server, signed_in, tmp_path, monkeypatch):
    monkeypatch.setattr(follow_trace, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(follow_trace, "_row", lambda: {"play_started_at": 100.0})
    res, body = call(server, "POST", "/follow/trace",
                     {"session": "s1", "sent": 5.0,
                      "events": [{"t": 1.0, "idx": 2, "heard": 3.1, "junk": 9,
                                  "off": [1]}]}, AUTH)
    assert res.status == 200 and body["kept"] == 1, body
    line = json.loads((tmp_path / "follow-trace.log").read_text().splitlines()[-1])
    assert line["events"] == [{"t": 1.0, "idx": 2, "heard": 3.1, "off": None}]
    assert line["row"] == {"play_started_at": 100.0} and line["session"] == "s1"


def test_it_needs_a_signed_in_caller(server, tmp_path, monkeypatch):
    monkeypatch.setattr(follow_trace, "state_dir", lambda: tmp_path)
    res, _ = call(server, "POST", "/follow/trace", {"events": []}, {})
    assert res.status in (401, 403)
    assert not (tmp_path / "follow-trace.log").exists()


def test_events_must_be_a_list(server, signed_in, tmp_path, monkeypatch):
    monkeypatch.setattr(follow_trace, "state_dir", lambda: tmp_path)
    res, _ = call(server, "POST", "/follow/trace", {"events": "x"}, AUTH)
    assert res.status == 400
