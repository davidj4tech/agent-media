"""The input box backend: auth, amux picker, reply-to-speaker delivery."""

import json

from agent_media_visual import canvas


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


def test_no_token_configured_denies_everything(tmp_path, monkeypatch):
    monkeypatch.delenv("AMUX_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(canvas.Path, "home", staticmethod(lambda: tmp_path))
    assert canvas._authorized(_Req({"X-Auth-Token": "anything"})) is False


def test_token_file_and_header_match(tmp_path, monkeypatch):
    monkeypatch.delenv("AMUX_AUTH_TOKEN", raising=False)
    (tmp_path / ".amux").mkdir()
    (tmp_path / ".amux" / "auth_token").write_text("sekrit\n")
    monkeypatch.setattr(canvas.Path, "home", staticmethod(lambda: tmp_path))
    assert canvas._authorized(_Req({"X-Auth-Token": "sekrit"})) is True
    assert canvas._authorized(_Req({"Authorization": "Bearer sekrit"})) is True
    assert canvas._authorized(_Req({"X-Auth-Token": "wrong"})) is False
    assert canvas._authorized(_Req()) is False


def test_env_token_wins(monkeypatch):
    monkeypatch.setenv("AMUX_AUTH_TOKEN", "envtok")
    assert canvas._authorized(_Req({"X-Auth-Token": "envtok"})) is True


def test_amux_ls_parsing(monkeypatch):
    monkeypatch.setattr(canvas, "_media_run", lambda argv, timeout=10: json.dumps([
        {"name": "scratch", "state": "working", "dir": "/home/x/scratch",
         "flags": ["YOLO"], "preview": "…"},
        {"name": "blog", "state": "stopped", "dir": "/home/x/blog"},
        {"state": "working"},          # nameless → dropped
        "not a session dict",          # junk entry → dropped
    ]))
    names = [s["name"] for s in canvas._amux_sessions()]
    assert names == ["scratch", "blog"]


def test_amux_ls_non_json_degrades_empty(monkeypatch):
    # An old amux without --json prints the plain table (or usage noise);
    # _amux_sessions must degrade to [] rather than raise.
    monkeypatch.setattr(canvas, "_media_run", lambda argv, timeout=10:
                        " 1  scratch          /home/x/scratch YOLO\n")
    assert canvas._amux_sessions() == []


def test_send_input_rejects_empty_and_unknown(monkeypatch):
    monkeypatch.setattr(canvas, "_amux_sessions",
                        lambda: [{"name": "scratch", "dir": "/x"}])
    assert canvas.send_input("", "speaker")[0] is False
    ok, detail = canvas.send_input("hi", "amux:nope")
    assert ok is False and "unknown amux session" in detail


def test_send_input_amux_route(monkeypatch):
    monkeypatch.setattr(canvas, "_amux_sessions",
                        lambda: [{"name": "scratch", "dir": "/x"}])
    seen = {}

    def fake_run(argv, timeout=10):
        seen["argv"] = argv
        return "sent"

    monkeypatch.setattr(canvas, "_media_run", fake_run)
    ok, detail = canvas.send_input("hello there", "amux:scratch")
    assert ok and detail == "amux:scratch"
    assert seen["argv"][0].endswith("amux")
    assert seen["argv"][1:] == ["send", "scratch", "hello there"]


def test_send_input_speaker_route(monkeypatch):
    monkeypatch.setattr(canvas, "_last_speaker",
                        lambda: {"pane": "%42", "session": "s",
                                 "tmux_session": "workbench"})
    seen = {}
    monkeypatch.setattr(canvas, "_send_to_pane",
                        lambda pane, text: seen.update(pane=pane, text=text) or "")
    ok, detail = canvas.send_input("follow-up", "speaker")
    assert ok and detail == "workbench"
    assert seen == {"pane": "%42", "text": "follow-up"}


def test_send_input_no_speaker(monkeypatch):
    monkeypatch.setattr(canvas, "_last_speaker", lambda: None)
    ok, detail = canvas.send_input("hi", "speaker")
    assert ok is False and "no speaker" in detail
