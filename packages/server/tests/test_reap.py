"""The idle reaper (`reap.py`), resting (`rest.py`), pins (`pins.py`) and the
archive import (`archive_import.py`).

Everything underneath is fake: the live sweep, the panes' screens, the speech
state, the memory (a fake /proc root from the conftest), transcripts (under
the throwaway CLAUDE_CONFIG_DIR / CODEX_HOME / PI_CODING_AGENT_DIR /
HERMES_HOME), the gateway and Audiobookshelf. Closing a pane goes to a
recorder; nothing here can reach tmux.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import time

import pytest

from agent_media_server import (app, archive, archive_import, drafts, panes, pins, procmem,
                                reap, recaps, rest, send, sessions)

from test_contract import (AUTH, SID, SID2, call, server, shelf,  # noqa: F401
                           signed_in, typed)

NOW = 1_790_000_000.0
H = 3600.0
S3 = "22222222-3333-4444-8555-666666666666"


# --- the rig ------------------------------------------------------------------------

@pytest.fixture()
def rig(monkeypatch, tmp_path):
    """Three live sessions on %1 %2 %3, each idle 13 h, each an idle Claude
    prompt with nothing typed, on a host with plenty of memory. Tests change
    one thing each."""
    for var, sub in (("CODEX_HOME", "codex"), ("PI_CODING_AGENT_DIR", "pi"),
                     ("HERMES_HOME", "hermes")):
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    state = {
        "live": {SID: "%1", SID2: "%2", S3: "%3"},
        "pids": {SID: 100, SID2: 200, S3: 300},
        "classes": {"%1": "input", "%2": "input", "%3": "input"},
        "drafted": set(),
        "last": {SID: NOW - 13 * H, SID2: NOW - 13 * H, S3: NOW - 13 * H},
        "spoken": {},
        "speech": (False, ""),
        "closed": [],
        "chat": [],
        "chat_reply": "We were wiring the reaper. Next: try it on red5.",
    }

    def live():
        sessions._PIDS = dict(state["pids"])
        return dict(state["live"])

    monkeypatch.setattr(sessions, "live_sessions", live)
    monkeypatch.setattr(sessions, "_pane_titles",
                        lambda: {"%1": "one", "%2": "two", "%3": "three"})
    monkeypatch.setattr(sessions, "_capture_pane", lambda p: f"screen of {p}")
    monkeypatch.setattr(sessions, "_agent_of_pane", lambda p: "claude")
    monkeypatch.setattr(panes, "classify",
                        lambda cap, agent="claude": state["classes"][cap.split()[-1]])
    monkeypatch.setattr(sessions, "pane_draft", lambda p: p in state["drafted"])
    monkeypatch.setattr(reap, "last_message_at", lambda s: state["last"].get(s))
    monkeypatch.setattr(reap, "speech_now", lambda: state["speech"])
    from agent_media_core.state import store

    monkeypatch.setattr(store.StateStore, "last_spoken", lambda self: dict(state["spoken"]))

    def close(session, pane=""):
        state["closed"].append((session, pane))
        return True, {"session": session, "pane": pane, "live": False, "closed": True}

    monkeypatch.setattr(send, "close_pane", close)
    from agent_media_core.intake import _summary

    def chat(prompt, text, timeout, model=None):
        state["chat"].append({"prompt": prompt, "text": text, "timeout": timeout,
                              "model": model})
        return state["chat_reply"]

    monkeypatch.setattr(_summary, "_chat", chat)
    monkeypatch.setattr(reap, "tail_text", lambda s, limit=6000: f"Person: what about {s[:4]}?")
    _meminfo(total_mb=8000, avail_mb=4000)
    return state


def _meminfo(total_mb: int, avail_mb: int) -> None:
    (procmem.PROC / "meminfo").write_text(
        f"MemTotal: {total_mb * 1024} kB\nMemAvailable: {avail_mb * 1024} kB\n")


def _decide(now: float = NOW) -> dict[str, reap.Decision]:
    decisions, _host = reap.decide(reap.Config.from_env(), now)
    return {d.session: d for d in decisions}


def _run(mode: str | None = None, **kw) -> str:
    buf = io.StringIO()
    reap.run(mode, now=NOW, out=buf, **kw)
    return buf.getvalue()


# --- the threshold -------------------------------------------------------------------

def test_defaults_and_env_tunables(monkeypatch):
    cfg = reap.Config.from_env()
    assert (cfg.idle_h, cfg.tight_idle_h, cfg.tight_pct, cfg.tight_mb, cfg.mode) == \
        (12.0, 6.0, 20.0, 1500.0, "dry-run")
    monkeypatch.setenv("MEDIA_REAP_IDLE_H", "10")
    monkeypatch.setenv("MEDIA_REAP_TIGHT_IDLE_H", "3.5")
    monkeypatch.setenv("MEDIA_REAP_TIGHT_PCT", "25")
    monkeypatch.setenv("MEDIA_REAP_TIGHT_MB", "2000")
    monkeypatch.setenv("MEDIA_REAP_MODE", "apply")
    cfg = reap.Config.from_env()
    assert (cfg.idle_h, cfg.tight_idle_h, cfg.tight_pct, cfg.tight_mb, cfg.mode) == \
        (10.0, 3.5, 25.0, 2000.0, "apply")
    monkeypatch.setenv("MEDIA_REAP_MODE", "yes please")        # nonsense → dry run
    monkeypatch.setenv("MEDIA_REAP_IDLE_H", "soon")            # nonsense → default
    cfg = reap.Config.from_env()
    assert cfg.mode == "dry-run" and cfg.idle_h == 12.0


def test_tight_memory_by_percentage_or_megabytes():
    cfg = reap.Config.from_env()
    assert not reap.is_tight({"mem_total_mb": 8000, "mem_available_mb": 4000}, cfg)
    assert reap.is_tight({"mem_total_mb": 8000, "mem_available_mb": 1400}, cfg)   # < 1.5 GB
    assert reap.is_tight({"mem_total_mb": 16000, "mem_available_mb": 3000}, cfg)  # < 20 %
    assert not reap.is_tight({"mem_total_mb": 16000, "mem_available_mb": 3300}, cfg)
    # Unreadable is not tight: the long threshold is the safe one.
    assert not reap.is_tight({"mem_total_mb": None, "mem_available_mb": None}, cfg)
    assert reap.threshold_h({"mem_total_mb": 8000, "mem_available_mb": 1000}, cfg) == 6.0
    assert reap.threshold_h({"mem_total_mb": 8000, "mem_available_mb": 4000}, cfg) == 12.0


def test_idle_past_twelve_hours_is_closed_and_under_is_kept(rig):
    rig["last"][SID2] = NOW - 11.9 * H
    got = _decide()
    assert got[SID].action == "close" and got[SID].rest_reason == "idle"
    assert got[SID].idle_h == 13.0
    assert got[SID2].action == "keep" and got[SID2].reasons == ["recent"]


def test_tight_memory_uses_the_short_threshold(rig):
    _meminfo(total_mb=8000, avail_mb=1200)
    rig["last"][SID2] = NOW - 7 * H
    rig["last"][S3] = NOW - 5 * H
    decisions, host = reap.decide(reap.Config.from_env(), NOW)
    got = {d.session: d for d in decisions}
    assert host["tight"] is True and host["threshold_h"] == 6.0
    assert got[SID2].action == "close" and got[SID2].rest_reason == "idle-tight"
    assert got[SID].rest_reason == "idle"         # past 12 h: idle either way
    assert got[S3].reasons == ["recent"]


def test_speech_history_counts_as_a_message(rig):
    rig["spoken"] = {SID: NOW - 1 * H}
    assert _decide()[SID].reasons == ["recent"]


# --- never reaped -------------------------------------------------------------------------

def test_pinned_is_never_reaped(rig):
    pins.set_pinned(SID, True)
    assert _decide()[SID].reasons == ["pinned"]


def test_the_callers_own_pane_is_never_reaped(rig, monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%2")
    assert _decide()[SID2].reasons == ["caller"]


def test_an_agent_the_reaper_runs_under_is_never_reaped(rig, monkeypatch):
    """No pane in the environment (a timer, a subprocess), but an ancestor is
    that session's agent process."""
    root = procmem.PROC
    me = os.getpid()
    for pid, ppid in ((me, 4242), (4242, 300), (300, 1)):
        (root / str(pid)).mkdir()
        (root / str(pid) / "stat").write_text(f"{pid} (x) S {ppid} 0 0\n")
    got = _decide()
    assert got[S3].reasons == ["caller"]
    assert got[SID].action == "close"


@pytest.mark.parametrize("live", [True, None])
def test_the_session_speech_is_from_is_never_reaped(rig, live):
    """Speaking or paused (True) — and when the speech state could not be
    read (None), the session it last named, failing closed."""
    rig["speech"] = (live, SID)
    assert _decide()[SID].reasons == ["speech-live"]


def test_finished_speech_protects_nothing(rig):
    rig["speech"] = (False, SID)
    assert _decide()[SID].action == "close"


@pytest.mark.parametrize("cls,reason", [("working", "working"), ("approval", "approval"),
                                        (None, "unrecognised")])
def test_what_the_pane_shows(rig, cls, reason):
    rig["classes"]["%1"] = cls
    assert _decide()[SID].reasons == [reason]


def test_activity_of_is_the_one_busy_answer(rig):
    """`sessions.activity_of` is what both /sessions/state and the reaper ask."""
    rig["classes"].update({"%1": "working", "%2": "approval", "%3": None})
    rig["drafted"].add("%2")
    assert sessions.activity_of(SID, "%1") == {"state": "working"}
    assert sessions.activity_of(SID2, "%2", with_draft=True) == \
        {"state": "approval", "draft": True}
    assert sessions.activity_of(S3, "%3", with_draft=True) == {"state": None, "draft": False}
    rig["classes"]["%1"] = "input"
    assert sessions.activity_of(SID, "%1")["state"] == "waiting"


def test_half_typed_text_in_the_composer(rig):
    rig["drafted"].add("%1")
    assert _decide()[SID].reasons == ["pane-draft"]


def test_a_fresh_server_draft_protects_and_a_stale_or_empty_one_does_not(rig):
    path = drafts._drafts_dir() / f"{SID}.json"
    path.write_text(json.dumps({"text": "half a thought", "at": 1}))
    os.utime(path, (NOW - 5 * H, NOW - 5 * H))
    assert _decide()[SID].reasons == ["server-draft"]
    os.utime(path, (NOW - 7 * H, NOW - 7 * H))
    assert _decide()[SID].action == "close"
    path.write_text(json.dumps({"text": "   ", "at": 1}))
    os.utime(path, (NOW - 1 * H, NOW - 1 * H))
    assert _decide()[SID].action == "close"


def test_no_known_last_message_is_kept(rig):
    rig["last"].pop(SID)
    d = _decide()[SID]
    assert d.reasons == ["no-last-message"] and d.idle_h is None


def test_every_reason_is_listed(rig, monkeypatch):
    pins.set_pinned(SID, True)
    rig["classes"]["%1"] = "working"
    rig["drafted"].add("%1")
    assert _decide()[SID].reasons == ["pinned", "working", "pane-draft"]


# --- dry run and apply ------------------------------------------------------------------------

def test_dry_run_closes_nothing_writes_nothing_and_logs_each_decision(rig):
    rig["last"][SID2] = NOW - 2 * H
    out = _run("dry-run")
    assert rig["closed"] == [] and rig["chat"] == []
    assert rest.rested() == {} and rest.generated_recap(SID) is None
    lines = out.splitlines()
    assert len(lines) == 3
    one = next(ln for ln in lines if SID[:8] in ln)
    assert " dry-run would-close " in one and '"one"' in one
    assert "idle=13.0h/12h" in one and "why=idle recap=would-write" in one
    assert "host=4000/8000MB" in one
    two = next(ln for ln in lines if SID2[:8] in ln)
    assert " dry-run kept " in two and "reason=recent" in two
    assert reap.log_path().read_text().splitlines() == lines


def test_no_log_prints_only(rig):
    _run("dry-run", write_log=False)
    assert not reap.log_path().exists()


def test_json_output(rig):
    rig["last"][SID2] = NOW - 2 * H
    obj = json.loads(_run("dry-run", as_json=True, write_log=False))
    assert obj["mode"] == "dry-run" and obj["host"]["threshold_h"] == 12.0
    by = {d["session"]: d for d in obj["decisions"]}
    assert by[SID]["result"] == "would-close" and by[SID]["recap"] == "would-write"
    assert by[SID2]["result"] == "kept" and by[SID2]["reasons"] == ["recent"]


def test_the_mode_comes_from_the_env_unless_a_flag_says(rig, monkeypatch):
    monkeypatch.setenv("MEDIA_REAP_MODE", "apply")
    _run("dry-run")
    assert rig["closed"] == []
    _run(None)
    assert {s for s, _p in rig["closed"]} == {SID, SID2, S3}


def test_apply_closes_marks_rested_and_writes_a_recap(rig):
    rig["last"][SID2] = NOW - 2 * H
    _meminfo(total_mb=8000, avail_mb=1000)
    rig["last"][S3] = NOW - 7 * H
    out = _run("apply")
    assert sorted(rig["closed"]) == sorted([(SID, "%1"), (S3, "%3")])
    marks = rest.rested()
    assert marks[SID]["reason"] == "idle" and marks[SID]["idle_h"] == 13.0
    assert marks[S3]["reason"] == "idle-tight"
    assert SID2 not in marks
    assert rest.generated_recap(SID)["text"] == rig["chat_reply"]
    assert rig["chat"][0]["prompt"] == reap.RECAP_PROMPT
    assert rig["chat"][0]["timeout"] == 20
    assert "apply closed " in out and "recap=written" in out


def test_the_recap_model_follows_the_followup_one(rig, monkeypatch):
    monkeypatch.setenv("MEDIA_FOLLOWUP_MODEL", "claude-haiku-4-5")
    _run("apply")
    assert rig["chat"][0]["model"] == "claude-haiku-4-5"


def test_the_recap_model_and_timeout_can_be_set(rig, monkeypatch):
    monkeypatch.setenv("MEDIA_FOLLOWUP_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("MEDIA_REAP_RECAP_MODEL", "tiny")
    monkeypatch.setenv("MEDIA_REAP_RECAP_TIMEOUT", "5")
    _run("apply")
    assert (rig["chat"][0]["model"], rig["chat"][0]["timeout"]) == ("tiny", 5)


def test_no_recap_is_written_when_one_is_newer_than_the_last_message(rig, monkeypatch):
    monkeypatch.setattr(recaps, "latest_recap",
                        lambda s: {"text": "claude's own", "at": NOW - 12 * H})
    out = _run("apply")
    assert rig["chat"] == []
    assert len(rig["closed"]) == 3 and "recap=have" in out


def test_a_failed_recap_never_blocks_the_close(rig):
    rig["chat_reply"] = None
    out = _run("apply")
    assert len(rig["closed"]) == 3 and "recap=failed" in out
    assert rest.generated_recap(SID) is None
    assert set(rest.rested()) == {SID, SID2, S3}


def test_recaps_can_be_switched_off(rig, monkeypatch):
    monkeypatch.setenv("MEDIA_REAP_RECAP", "0")
    out = _run("apply")
    assert rig["chat"] == [] and len(rig["closed"]) == 3 and "recap=off" in out


def test_the_pane_is_looked_at_again_after_the_recap(rig, monkeypatch):
    """Somebody started typing while the gateway was answering."""
    from agent_media_core.intake import _summary

    def slow_chat(prompt, text, timeout, model=None):
        rig["drafted"].add("%1")
        return "recap"

    monkeypatch.setattr(_summary, "_chat", slow_chat)
    rig["last"][SID2] = rig["last"][S3] = NOW
    _run("apply")
    assert rig["closed"] == []
    assert rest.rested() == {}


def test_a_close_that_failed_is_not_marked_and_fails_the_run(rig, monkeypatch):
    monkeypatch.setattr(send, "close_pane",
                        lambda s, p="": (False, {"error": f"could not close {p}"}))
    buf = io.StringIO()
    assert reap.run("apply", now=NOW, out=buf) == 1
    assert rest.rested() == {}
    assert "failed: could not close %1" in buf.getvalue()


def test_a_session_gone_by_close_time_is_not_marked(rig, monkeypatch):
    monkeypatch.setattr(send, "close_pane",
                        lambda s, p="": (True, {"session": s, "live": False, "closed": False}))
    _run("apply")
    assert rest.rested() == {}


def test_apply_drops_a_stale_mark_on_a_session_that_is_live_again(rig):
    rest.mark_rested(SID, 13, "idle", at=NOW - 20 * H)
    rest.mark_rested("99999999-3333-4444-8555-666666666666", 13, "idle")
    rig["last"] = {k: NOW for k in rig["last"]}
    _run("dry-run")
    assert SID in rest.rested()                  # a dry run writes nothing
    _run("apply")
    assert set(rest.rested()) == {"99999999-3333-4444-8555-666666666666"}


def test_only_live_agent_sessions_are_considered(rig):
    """The candidates are exactly `live_sessions`' — agent processes in a pane."""
    rig["live"] = {SID: "%1"}
    assert set(_decide()) == {SID}


def test_no_live_sessions(rig):
    rig["live"] = {}
    assert "no live agent sessions" in _run("dry-run")


# --- close_pane, the path the reaper and /session/close share ---------------------------------

def test_close_pane_kills_only_the_pane_the_session_is_still_in(typed, no_retag, monkeypatch):
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%7"})
    ok, detail = send.close_pane(SID, "%9")               # it moved: leave it
    assert ok and detail["closed"] is False and typed == []
    ok, detail = send.close_pane(SID, "%7")
    assert ok and detail["closed"] is True
    assert ("_tmux", (["kill-pane", "-t", "%7"],)) in typed


# --- rested on the rows, and cleared -------------------------------------------------------

def test_rows_carry_rested_until_the_session_is_live(server, shelf, signed_in):
    rest.mark_rested(SID, 13.2, "idle-tight", at=NOW)
    _, obj = call(server, "GET", "/targets", headers=AUTH)
    by = {r["session"]: r for r in obj["sessions"]}
    assert by[SID]["rested"] == {"at": NOW, "reason": "idle-tight"}
    assert by[SID2]["rested"] is None
    _, conv = call(server, "GET", "/conversations", headers=AUTH)
    assert {r["session"]: r["rested"] for r in conv["sessions"]} == \
        {r["session"]: r["rested"] for r in obj["sessions"]}
    # Live again (resumed at the desk): the row says so whatever the file does.
    rest.mark_rested(SID2, 13, "idle")
    _, obj = call(server, "GET", "/targets", headers=AUTH)
    assert {r["session"]: r for r in obj["sessions"]}[SID2]["rested"] is None


def test_a_reply_clears_the_mark(server, shelf, signed_in, typed, monkeypatch):
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%42"})
    rest.mark_rested(SID, 13, "idle")
    res, obj = call(server, "POST", "/reply", {"session": SID, "text": "back to this"}, AUTH)
    assert res.status == 200, obj
    assert SID not in rest.rested()


def test_a_failed_send_leaves_the_mark(server, shelf, signed_in, typed, monkeypatch):
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%42"})
    monkeypatch.setattr(send, "_ensure_submitted", lambda *a, **k: False)
    rest.mark_rested(SID, 13, "idle")
    call(server, "POST", "/reply", {"session": SID, "text": "lost"}, AUTH)
    assert SID in rest.rested()


@pytest.fixture()
def no_retag(monkeypatch):
    """A close or a resume re-tags the ABS items in the background
    (`send._retag` → `book_tracks.sync_tags`), which would reach the host's
    real Audiobookshelf. Never from a test."""
    monkeypatch.setattr(send, "_retag", lambda s: None)


def test_a_resume_clears_the_mark(server, shelf, signed_in, typed, no_retag, monkeypatch):
    monkeypatch.setattr(send, "open_window", lambda *a, **k: ("%50", ""))
    monkeypatch.setattr(sessions, "live_sessions", lambda: {})
    rest.mark_rested(SID, 13, "idle")
    res, obj = call(server, "POST", "/session/resume", {"session": SID}, AUTH)
    assert res.status == 200 and obj["opened"] is True
    assert SID not in rest.rested()


def test_a_close_by_the_user_is_not_resting(server, shelf, signed_in, typed, no_retag):
    rest.mark_rested(SID2, 13, "idle")        # a stale mark from an earlier reap
    res, obj = call(server, "POST", "/session/close", {"session": SID2}, AUTH)
    assert res.status == 200 and obj["closed"] is True, obj
    assert rest.rested() == {}
    res, obj = call(server, "POST", "/session/close", {"session": SID}, AUTH)
    assert rest.rested() == {}


def test_rest_reasons_are_checked():
    with pytest.raises(ValueError):
        rest.mark_rested(SID, 1, "bored")


# --- recaps: Claude's own, or ours -------------------------------------------------------

def test_recap_for_prefers_the_newer_and_says_whose(monkeypatch):
    own = {"text": "claude's", "at": 100.0}
    monkeypatch.setattr(recaps, "latest_recap", lambda s: dict(own) if own else None)
    assert recaps.recap_for(SID) == {"text": "claude's", "at": 100.0, "source": "claude"}
    rest.save_recap(SID, "ours", at=200.0)
    assert recaps.recap_for(SID) == {"text": "ours", "at": 200.0, "source": "agent-media"}
    own["at"] = 300.0
    assert recaps.recap_for(SID)["source"] == "claude"
    own.clear()
    assert recaps.recap_for(SID) == {"text": "ours", "at": 200.0, "source": "agent-media"}
    assert recaps.recap_for(SID2) is None


def test_a_generated_recap_shows_on_the_rows_and_the_log(server, shelf, signed_in, monkeypatch):
    from agent_media_core import activity, book_tracks

    rest.save_recap(SID, "where this thread was", at=1789974112.0)
    _, obj = call(server, "GET", "/targets", headers=AUTH)
    assert {r["session"]: r for r in obj["sessions"]}[SID]["recap"] == \
        {"text": "where this thread was", "at": 1789974112.0, "source": "agent-media"}
    monkeypatch.setattr(book_tracks, "conversation_log",
                        lambda s, f, target=None, positions=True: [])
    monkeypatch.setattr(activity, "attach", lambda s, lines: None)
    res, obj = call(server, "GET", f"/conversation/log?session={SID}", headers=AUTH)
    assert res.status == 200, obj
    assert obj["recap"]["source"] == "agent-media"


# --- the last message ------------------------------------------------------------------------

def _jsonl(path, recs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))


def test_last_message_in_a_claude_transcript_skips_bookkeeping(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    recs = [{"type": "user", "timestamp": "2026-09-21T06:00:00Z",
             "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "timestamp": "2026-09-21T06:00:05.500Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}}]
    # A recap and a title written later are not messages.
    recs += [{"type": "system", "subtype": "away_summary", "content": "x",
              "timestamp": "2026-09-21T09:00:00Z"},
             {"type": "custom-title", "customTitle": "t"}]
    _jsonl(tmp_path / "claude" / "projects" / "p" / f"{SID}.jsonl", recs)
    assert reap.last_message_at(SID) == 1789970405.5
    assert reap.last_message_at(SID2) is None


def test_last_message_backwards_across_chunks(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    recs = [{"type": "user", "timestamp": "2026-09-21T06:00:00Z",
             "message": {"role": "user", "content": "x" * 5000}}]
    recs += [{"type": "progress", "data": "y" * 3000} for _ in range(200)]
    path = tmp_path / "claude" / "projects" / "p" / f"{SID}.jsonl"
    _jsonl(path, recs)
    lines = list(reap._lines_backwards(path, chunk=4096))
    assert len(lines) == 201 and json.loads(lines[-1])["type"] == "user"
    assert reap.last_message_at(SID) == 1789970400.0


def test_last_message_in_codex_and_pi(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    _jsonl(tmp_path / "codex" / "sessions" / "2026" / "09" / "21" / f"rollout-x-{SID}.jsonl", [
        {"timestamp": "2026-09-21T06:00:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "content": []}},
        {"timestamp": "2026-09-21T07:00:00Z", "type": "event_msg",
         "payload": {"type": "token_count"}}])
    assert reap.last_message_at(SID) == 1789970400.0
    _jsonl(tmp_path / "pi" / "sessions" / "d" / f"2026_{SID2}.jsonl", [
        {"type": "message", "timestamp": "2026-09-21T06:30:00Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}},
        {"type": "session_info", "name": "n", "timestamp": "2026-09-21T08:00:00Z"}])
    assert reap.last_message_at(SID2) == 1789972200.0


def test_last_message_in_hermes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    (tmp_path / "hermes").mkdir()
    hid = "20260921_102508_f74b02"
    with sqlite3.connect(tmp_path / "hermes" / "state.db") as c:
        c.execute("create table messages (id integer primary key, session_id text, "
                  "role text, content text, timestamp real)")
        c.executemany("insert into messages (session_id, role, content, timestamp) "
                      "values (?, ?, ?, ?)",
                      [(hid, "user", "hi", 100.0), (hid, "assistant", "yo", 200.0),
                       (hid, "tool", "{}", 300.0), ("other", "user", "x", 999.0)])
    assert reap.last_message_at(hid) == 200.0
    assert reap.tail_text(hid) == "Person: hi\nAssistant: yo"


def test_last_spoken_per_session_from_the_speech_history(tmp_path):
    from agent_media_core.state.store import StateStore

    st = StateStore(tmp_path / "state.db")
    st.add_history(sink="speech", uri="a", started_at=100.0, extras={"source_session": SID})
    st.add_history(sink="speech", uri="b", started_at=300.0, extras={"source_session": SID})
    st.add_history(sink="speech", uri="c", started_at=200.0, extras={"source_session": SID2})
    st.add_history(sink="music", uri="d", started_at=900.0, extras={"source_session": SID2})
    st.add_history(sink="speech", uri="e", started_at=950.0)          # nobody's
    assert st.last_spoken() == {SID: 300.0, SID2: 200.0}


def test_tail_text_reads_the_end_of_a_claude_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    _jsonl(tmp_path / "claude" / "projects" / "p" / f"{SID}.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "first ask"}},
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash"}, {"type": "text", "text": "done it"}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "noise"}]}}])
    assert reap.tail_text(SID) == "Person: first ask\nAssistant: done it"
    assert reap.tail_text(SID, limit=15) == "Assistant: done it"


# --- the pin ---------------------------------------------------------------------------------

def test_pin_toggles_and_shows_on_the_rows(server, shelf, signed_in, typed):
    res, obj = call(server, "POST", "/session/pin", {"session": SID2, "pinned": True}, AUTH)
    assert res.status == 200 and obj == {"ok": True, "session": SID2, "pinned": True}
    _, targets = call(server, "GET", "/targets", headers=AUTH)
    by = {r["session"]: r for r in targets["sessions"]}
    assert by[SID2]["pinned"] is True and by[SID]["pinned"] is False
    assert typed == []                                        # pinning types nothing
    res, obj = call(server, "POST", "/session/pin", {"session": SID2, "pinned": False}, AUTH)
    assert obj == {"ok": True, "session": SID2, "pinned": False}
    assert not pins.is_pinned(SID2)
    res, obj = call(server, "POST", "/session/pin", {"session": SID}, AUTH)
    assert obj["pinned"] is True                              # absent means pin


def test_pin_refusals(server, shelf, signed_in, monkeypatch):
    res, obj = call(server, "POST", "/session/pin", {"session": "nope"}, AUTH)
    assert res.status == 400 and obj == {"ok": False, "error": "not a session id"}
    res, obj = call(server, "POST", "/session/pin", {"session": SID, "pinned": 1}, AUTH)
    assert res.status == 400 and obj["error"] == "pinned must be true or false"
    monkeypatch.setattr(sessions, "session_exists", lambda s: False)
    other = "11111111-2222-4333-8444-555555555555"
    res, obj = call(server, "POST", "/session/pin", {"session": other}, AUTH)
    assert res.status == 404 and obj == {"ok": False, "error": "no such session 11111111"}
    assert not pins.PINS.path().exists()


def test_pin_is_gated_and_open_to_the_web_client(server, shelf, monkeypatch):
    from agent_media_server import auth_abs

    monkeypatch.setattr(auth_abs, "abs_urls", lambda: ["http://abs"])
    monkeypatch.setattr(auth_abs, "_abs_get", lambda *a, **k: (None, 401))
    res, obj = call(server, "POST", "/session/pin", {"session": SID})
    assert res.status == 401 and obj["ok"] is False
    assert not pins.PINS.path().exists()
    assert "/session/pin" in app.CORS_PATHS


def test_pins_persist_and_survive_another_process(monkeypatch):
    assert pins.set_pinned(SID, True) is True
    assert pins.set_pinned(SID, True) is False            # no change, no write
    pins.PINS.path().write_text(json.dumps({SID2: 1.0}))  # another process wrote
    assert pins.pinned() == {SID2: 1.0}
    assert pins.set_pinned(SID2, False) is True and pins.pinned() == {}


# --- always speak ----------------------------------------------------------------------------

def test_priority_toggles_and_shows_on_the_rows(server, shelf, signed_in, typed):
    from agent_media_core import speak_priority

    res, obj = call(server, "POST", "/session/priority",
                    {"session": SID2, "priority": True}, AUTH)
    assert res.status == 200 and obj == {"ok": True, "session": SID2, "level": "auto",
                                         "priority": True}
    _, targets = call(server, "GET", "/targets", headers=AUTH)
    by = {r["session"]: r for r in targets["sessions"]}
    assert by[SID2]["priority"] is True and by[SID]["priority"] is False
    assert by[SID2]["pinned"] is False                        # not a pin
    assert typed == []
    res, obj = call(server, "POST", "/session/priority",
                    {"session": SID2, "priority": False}, AUTH)
    assert obj == {"ok": True, "session": SID2, "level": "normal", "priority": False}
    assert not speak_priority.is_priority(SID2)
    res, obj = call(server, "POST", "/session/priority", {"session": "nope"}, AUTH)
    assert res.status == 400
    res, obj = call(server, "POST", "/session/priority", {"session": SID, "priority": 1}, AUTH)
    assert res.status == 400 and obj["error"] == "priority must be true or false"
    assert "/session/priority" in app.CORS_PATHS


def test_speech_level_is_set_and_shows_on_the_rows(server, shelf, signed_in, typed):
    for level, prio in (("interrupt", True), ("quiet", False), ("auto", True)):
        res, obj = call(server, "POST", "/session/priority", {"session": SID2, "level": level}, AUTH)
        assert res.status == 200 and obj == {"ok": True, "session": SID2, "level": level,
                                             "priority": prio}
        _, targets = call(server, "GET", "/targets", headers=AUTH)
        row = next(r for r in targets["sessions"] if r["session"] == SID2)
        assert row["speech"] == level and row["priority"] is prio
    res, obj = call(server, "POST", "/session/priority", {"session": SID2, "level": "loud"}, AUTH)
    assert res.status == 400 and obj["error"] == "level must be interrupt, auto, normal or quiet"
    call(server, "POST", "/session/priority", {"session": SID2, "level": "normal"}, AUTH)


# --- the archive import ---------------------------------------------------------------------

@pytest.fixture()
def abs_fake(monkeypatch, tmp_path):
    """A conversations library of four items and an audiobook library, on one
    server. Any write to ABS fails the test."""
    from agent_media_core import book_tracks

    d = tmp_path / "book-tracks"
    d.mkdir(exist_ok=True)
    for sid, folder in ((SID, "/lib/Conversations/p-agent-media/Sasonica music"),
                        (SID2, "/lib/Conversations/p-agent-media/Sasonica web"),
                        (S3, "/lib/Conversations/p-sasonica/Player")):
        (d / f"{sid}.json").write_text(json.dumps({"session": sid, "folder": folder}))
    monkeypatch.setattr(sessions, "_manifest_dir", lambda: d)

    def item(i, path, tags, title=""):
        return {"id": i, "path": path,
                "media": {"tags": tags, "metadata": {"title": title or path.split("/")[-1]}}}

    libs = {
        "lib-conv": [item("li_1", "/conversations/p-agent-media/Sasonica music", ["archived"]),
                     item("li_2", "/conversations/p-agent-media/Sasonica web", ["live"]),
                     item("li_3", "/conversations/p-sasonica/Player", ["archived", "x"]),
                     item("li_4", "/conversations/p-old/Before manifests", ["archived"])],
        "lib-books": [item("li_9", "/audiobooks/Author/A Book", ["archived"])],
    }
    asked = []

    def items(url, token, lib_id):
        asked.append((url, lib_id))
        return libs[lib_id]

    monkeypatch.setattr(book_tracks, "_abs_items", items)

    def no_writes(*a, **k):
        raise AssertionError("the import wrote to Audiobookshelf")

    monkeypatch.setattr(book_tracks, "_abs_patch", no_writes)
    servers = [("http://abs", "tok", [{"id": "lib-conv", "name": "Conversations"},
                                      {"id": "lib-books", "name": "Audiobooks"}])]
    return servers, asked


def test_import_maps_archived_items_to_sessions(abs_fake):
    servers, asked = abs_fake
    archive.set_archived(S3, True)
    rows, problems = archive_import.plan(servers)
    assert problems == []
    by = {r["item"]: r for r in rows}
    assert set(by) == {"li_1", "li_3", "li_4"}          # the audiobook is not reported
    assert by["li_1"]["session"] == SID and by["li_1"]["state"] == "mark"
    assert by["li_3"]["session"] == S3 and by["li_3"]["state"] == "already"
    assert by["li_4"]["session"] is None and by["li_4"]["state"] == "unmapped"
    assert by["li_1"]["tail"] == "p-agent-media/Sasonica music"


def test_import_is_a_dry_run_unless_applied(abs_fake):
    servers, _ = abs_fake
    buf = io.StringIO()
    assert archive_import.run(servers=servers, out=buf) == 0
    assert not archive.is_archived(SID)
    text = buf.getvalue()
    assert "would mark" in text and SID[:8] in text and "run with --apply" in text
    buf = io.StringIO()
    archive_import.run(apply=True, servers=servers, out=buf)
    assert archive.is_archived(SID) and not archive.is_archived(SID2)
    assert "marked" in buf.getvalue()
    obj_buf = io.StringIO()
    archive_import.run(servers=servers, as_json=True, out=obj_buf)
    states = {r["item"]: r["state"] for r in json.loads(obj_buf.getvalue())["rows"]}
    assert states["li_1"] == "already"


def test_import_marks_a_session_once_across_servers(abs_fake):
    servers, _ = abs_fake
    rows, _ = archive_import.plan(servers + servers)
    assert [r["session"] for r in rows if r["state"] == "mark"] == [SID, S3]
    assert [r["item"] for r in rows if r["state"] == "unmapped"] == ["li_4"]


def test_import_reports_a_library_that_did_not_answer(abs_fake, monkeypatch):
    from agent_media_core import book_tracks

    servers, _ = abs_fake

    def boom(url, token, lib_id):
        raise OSError("timed out")

    monkeypatch.setattr(book_tracks, "_abs_items", boom)
    buf = io.StringIO()
    assert archive_import.run(servers=servers, out=buf) == 1
    assert "problem: http://abs library Conversations: timed out" in buf.getvalue()


# --- the CLI ---------------------------------------------------------------------------------

def _media(argv: list[str]) -> int:
    """`media <argv>` without `main`'s env-file load, which would pull the
    machine's real agent-media.env into this process."""
    from agent_media_core import cli

    ns = cli._build_parser().parse_args(argv)
    return ns.func(ns)


def test_media_session_reap_dispatches(monkeypatch):
    seen = []
    monkeypatch.setattr(reap, "run", lambda mode, as_json=False, write_log=True:
                        seen.append((mode, as_json, write_log)) or 0)
    assert _media(["session-reap"]) == 0
    assert _media(["session-reap", "--apply", "--json"]) == 0
    assert _media(["session-reap", "--dry-run", "--no-log"]) == 0
    assert seen == [(None, False, True), ("apply", True, True), ("dry-run", False, False)]
    with pytest.raises(SystemExit):
        _media(["session-reap", "--apply", "--dry-run"])


def test_media_session_archive_import_dispatches(monkeypatch):
    seen = []
    monkeypatch.setattr(archive_import, "run", lambda apply=False, as_json=False:
                        seen.append((apply, as_json)) or 0)
    assert _media(["session-archive-import"]) == 0
    assert _media(["session-archive-import", "--apply"]) == 0
    assert seen == [(False, False), (True, False)]


def test_the_timer_template_runs_the_reaper():
    from agent_media_core import setup

    src = setup.service_templates_dir() / "session-reap"
    assert "exec media session-reap" in (src / "run").read_text()
    assert "OnCalendar=*:0/15" in (src / "timer").read_text()
    assert "requires: origin" in (src / "roles").read_text()


def test_gateway_sessions_are_machinery_not_threads(monkeypatch, tmp_path):
    """Meridian's pool lives in ~/.meridian; its agents are never threads and
    never reaped. The check is on the agent process's own working folder."""
    import os

    from agent_media_server import sessions

    monkeypatch.delenv("MEDIA_SESSIONS_EXCLUDE_CWD", raising=False)
    assert os.path.realpath(os.path.expanduser("~/.meridian")) in sessions._excluded_dirs()
    here = os.path.realpath(os.getcwd())
    monkeypatch.setenv("MEDIA_SESSIONS_EXCLUDE_CWD", here)
    assert sessions._is_machinery(os.getpid(), sessions._excluded_dirs())
    monkeypatch.setenv("MEDIA_SESSIONS_EXCLUDE_CWD", str(tmp_path / "elsewhere"))
    assert not sessions._is_machinery(os.getpid(), sessions._excluded_dirs())
    monkeypatch.setenv("MEDIA_SESSIONS_EXCLUDE_CWD", "")
    assert sessions._excluded_dirs() == []
