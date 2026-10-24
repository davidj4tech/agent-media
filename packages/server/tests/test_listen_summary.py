"""The listen-log summary (listen_summary.py): why listens came back empty."""

from __future__ import annotations

import json
import time

from agent_media_server import listen_summary as ls


def _empty(**kw):
    return {"at": "handsfree", "outcome": "empty", "received": time.time(), **kw}


LINES = [
    _empty(),                                                                # an older build: no diagnostics
    _empty(recError=7, openMs=1500, speechMs=-1, ranMs=6000, chime=True, rmsPeak=1.2),
    _empty(recError=7, openMs=-1, speechMs=-1, ranMs=800, chime=True),
    _empty(recError=6, openMs=4100, speechMs=-1, ranMs=3000, chime=True, rmsPeak=8.0, playingAtStart=True),
    _empty(recError=7, openMs=1400, speechMs=900, ranMs=4000, chime=False, rmsPeak=9.5, interlude=True),
    {"at": "handsfree", "outcome": "taken", "text": "yes", "received": time.time()},
    {"at": "dictation", "outcome": "empty", "openMs": -1, "received": time.time()},
]


def test_empties_are_told_apart():
    s = ls.summarise(LINES, "handsfree")
    assert s["listens"] == 6 and s["empty"] == 5
    assert s["empty_with_diagnostics"] == 4 and s["empty_without_diagnostics"] == 1
    assert s["main_cause"] == {"no_speech": 1, "never_opened": 1, "slow_open": 1, "speech_no_words": 1}
    assert s["also"] == {"quiet": 1, "voice_playing": 1, "interlude": 1, "no_chime": 1}
    assert s["rec_errors"] == {"7": 3, "6": 1}
    assert s["open_ms"]["n"] == 3 and s["open_ms"]["max"] == 4100


def test_the_text_says_it():
    text = ls.render(ls.summarise(LINES, "handsfree"))
    assert "5 empty, 4 with diagnostics (1 from a build that did not send them)" in text
    assert "mic never opened (openMs -1)" in text and "quiet (rmsPeak under 3.0 dB)" in text


def test_no_empties_and_a_missing_log(tmp_path, capsys):
    assert "no empty listens" in ls.render(ls.summarise([{"outcome": "taken", "at": "handsfree"}]))
    assert ls.main(["--file", str(tmp_path / "none.jsonl")]) == 1


def test_the_file_and_the_window(tmp_path, capsys):
    p = tmp_path / "listen-log.jsonl"
    old = {**_empty(openMs=-1), "received": time.time() - 7200}
    p.write_text("\n".join([json.dumps(old), "not json", json.dumps(LINES[1])]) + "\n")
    assert len(ls.read(p)) == 2
    assert len(ls.read(p, since=time.time() - 3600)) == 1
    assert ls.main(["--file", str(p), "--hours", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["empty"] == 1
