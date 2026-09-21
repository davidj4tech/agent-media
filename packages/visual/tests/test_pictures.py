"""Pictures on the transcript: the canvas's spool, joined to the server's log.

The conversation log lives in the server package, the spool in this one, so
the join goes through the callback the canvas registers at import
(`threads.set_pictures_for(state.pictures_for)`).
"""

from agent_media_server import threads

from agent_media_visual import canvas, state


def test_the_canvas_registers_its_picture_lookup():
    assert canvas and threads._PICTURES_FOR is state.pictures_for


def test_a_reply_gets_the_picture_the_canvas_drew_for_it(tmp_path, monkeypatch):
    (tmp_path / "img-1.svg").write_text("<svg/>")
    monkeypatch.setattr(state, "spool_dir", lambda: tmp_path)
    pushes = {"k1": {"image": "img-1.svg", "purpose": "figure"},
              "k2": {"sequence": [{"image": "gone.svg", "at": 0},
                                  {"image": "http://other/img/x.svg", "at": 0.5}]}}
    monkeypatch.setattr(state, "load_push", lambda k: pushes.get(k))
    monkeypatch.setattr(threads, "_PICTURES_FOR", state.pictures_for)
    lines = [{"who": "agent", "text": "a", "key": "k1"},
             {"who": "you", "text": "b", "key": ""},
             {"who": "agent", "text": "c", "key": "k2"},
             {"who": "agent", "text": "d", "key": "unknown"}]
    threads.attach_pictures(lines)
    assert lines[0]["images"] == ["/img/img-1.svg"] and lines[0]["figure"] is True
    assert "images" not in lines[1]
    # A swept spool file is left out; another host's absolute URL is passed on.
    assert lines[2]["images"] == ["http://other/img/x.svg"] and lines[2]["figure"] is False
    assert "images" not in lines[3]
