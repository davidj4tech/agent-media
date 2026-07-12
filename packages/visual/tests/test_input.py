"""The input box backend: auth, amux picker, reply-to-speaker delivery."""

import json
import re
import time

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


def test_wake_target_picks_freshest_viewer():
    canvas._VIEWERS.clear()
    canvas._VIEWERS["sp4"] = (time.time() - 3600, True)
    canvas._viewer_seen("hpo")
    assert canvas._wake_target() == "hpo"


def test_wake_target_skips_blurred_screens():
    # Freshest screen's canvas lost the foreground (page beaconed blur) —
    # fall through to the older screen whose canvas is still up front.
    canvas._VIEWERS.clear()
    canvas._VIEWERS["hpo"] = (time.time() - 3600, True)
    canvas._viewer_seen("sp4", focused=False)
    assert canvas._wake_target() == "hpo"
    canvas._VIEWERS.clear()
    canvas._viewer_seen("sp4", focused=False)   # only viewer, blurred
    assert canvas._wake_target() is None


def test_wake_target_skips_ignored_screens(monkeypatch):
    monkeypatch.setenv("MEDIA_VISUAL_WAKE_IGNORE", "p8a, ftv")
    canvas._VIEWERS.clear()
    canvas._VIEWERS["hpo"] = (time.time() - 3600, True)
    canvas._viewer_seen("p8a")            # freshest, but ignored
    assert canvas._wake_target() == "hpo"
    canvas._VIEWERS.clear()
    canvas._viewer_seen("P8A")            # case-insensitive, only viewer
    assert canvas._wake_target() is None


def test_wake_target_none_when_empty_or_stale():
    canvas._VIEWERS.clear()
    assert canvas._wake_target() is None
    canvas._VIEWERS["hpo"] = (time.time() - 90000, True)   # > 12h window
    assert canvas._wake_target() is None


def test_screen_from_ip_parses_and_caches(monkeypatch):
    canvas._WHOIS_CACHE.clear()
    calls = []

    class _R:
        returncode = 0
        stdout = json.dumps({"Node": {"ComputedName": "hpo.tail.ts.net"}})

    def fake_run(argv, **kw):
        calls.append(argv)
        return _R()

    monkeypatch.setattr(canvas.subprocess, "run", fake_run)
    assert canvas._screen_from_ip("100.1.2.3") == "hpo"
    assert canvas._screen_from_ip("100.1.2.3") == "hpo"   # served from cache
    assert len(calls) == 1


def test_screen_from_ip_unresolvable(monkeypatch):
    canvas._WHOIS_CACHE.clear()

    def fake_run(argv, **kw):
        raise OSError("no tailscale here")

    monkeypatch.setattr(canvas.subprocess, "run", fake_run)
    assert canvas._screen_from_ip("192.168.1.9") == ""


def test_viewer_seen_sanitizes_names():
    canvas._VIEWERS.clear()
    canvas._viewer_seen("héllo wörld/../x" + "y" * 64)
    canvas._viewer_seen("")            # empty → not registered
    canvas._viewer_seen("!!!")         # nothing survives the strip → dropped
    (name,) = canvas._VIEWERS
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,32}", name)


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
