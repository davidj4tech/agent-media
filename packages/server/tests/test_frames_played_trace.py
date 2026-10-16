import json
from agent_media_server import speech_frames as F
def test_played_is_traced_once(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    lines=[]
    monkeypatch.setattr(F, "trace", lambda ev, hub, **kw: lines.append((ev, kw)))
    hub = F._Hub("speech")
    ex = {"played": [{"i": 5, "id": "a--005", "at": 1000}, {"i": 6, "id": "a--008", "expected": "a--006", "at": 2000}]}
    F._trace_played(hub, ex); F._trace_played(hub, ex)
    assert [k["i"] for _, k in lines] == [5, 6]
    assert lines[1][1]["expected"] == "a--006"
    F._trace_played(hub, None); F._trace_played(hub, {"made": []})
    assert len(lines) == 2
