"""A Grok pane, its folder, and what sealing one of its threads takes.

Pane text is read from Grok 1.0.62's docs and binary, not yet off a
signed-in pane. The conversation is the transcript tests' fixture, copied
under the throwaway GROK_HOME the conftest sets.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from urllib.parse import quote

import pytest

from agent_media_server import panes, seal, send, sessions

SID = "0199f0a1-2b3c-7d4e-8f50-6172839405a6"
FIXTURE = Path(__file__).parent / "fixtures" / "grok"


@pytest.fixture
def chat(tmp_path, monkeypatch):
    home = tmp_path / "grok-home"
    monkeypatch.setenv("GROK_HOME", str(home))
    folder = home / "sessions" / quote("/home/x/proj", safe="") / SID
    folder.mkdir(parents=True)
    for f in ("summary.json", "updates.jsonl"):
        shutil.copy(FIXTURE / f, folder / f)
    return folder


def test_its_folder_and_what_sealing_takes(chat):
    assert sessions.transcript_cwd(SID) == "/home/x/proj"
    child = "0199f0a1-0000-7000-8000-00000000000c"
    (chat.parent / child).mkdir()
    (chat.parent / child / "summary.json").write_text("{}")
    (chat / "subagents" / "explore-1").mkdir(parents=True)
    (chat / "subagents" / "explore-1" / "meta.json").write_text(json.dumps({"session_id": child}))
    harness, paths = seal._transcript_paths(SID)
    assert harness == "grok" and chat in paths and chat.parent / child in paths
    assert chat / "updates.jsonl" not in paths      # inside the folder already


@pytest.mark.parametrize("screen,want", [
    ("Run pytest -q?\n> Allow once\n  Always allow: pytest\n  Reject once", "approval"),
    ("⠋ Thinking…\nPress Ctrl+c to cancel the turn", "working"),
    ("$ ls\nfoo bar", None),
])
def test_its_pane(screen, want):
    assert panes.classify(screen, "grok") == want


def test_its_pane_is_found_by_argv(monkeypatch):
    """Started by the download's own name, the pane does not say `grok`."""
    from agent_media_core import harnesses

    procs = {"10": ["-zsh"], "11": ["/home/x/.grok/downloads/grok-linux-x86_64"],
             "20": ["grok", "update"]}
    kids = {"10": ["11"]}
    monkeypatch.setattr(harnesses, "_argv", lambda pid: procs.get(pid, []))
    monkeypatch.setattr(panes, "_children", lambda pid: kids.get(pid, []))
    assert panes.agent_by_argv("10") == "grok"
    assert panes.agent_by_argv("20") == ""
    assert "grok" in panes.AGENT_COMMANDS


def test_a_new_chat_is_not_started_in_it_yet():
    """Its proposal's later phase: /ask refuses it until its fresh session
    and composer are measured."""
    assert "grok" in send.NOT_STARTED_HERE


def test_the_reaper_reads_the_fold(chat):
    """Its last message's last line and the recap's text, from the chunks
    joined."""
    from agent_media_server import reap

    assert reap.last_message_at(SID) == reap._epoch("2026-10-17T00:01:03.000Z")
    text = reap.tail_text(SID)
    assert text.splitlines() == ["Person: Run the fast tests",
                                 "Assistant: I'll run them. Done: **12 passed**.",
                                 "Person: Now read the README",
                                 "Assistant: It says hi."]
