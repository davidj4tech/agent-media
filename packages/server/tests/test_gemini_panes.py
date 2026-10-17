"""A Gemini CLI pane, its folder, and what sealing one of its threads takes.

Pane text is read from gemini-cli 0.64's bundle, not yet off a signed-in
pane. The conversation is the transcript tests' fixture, copied under the
throwaway GEMINI_CLI_HOME the conftest sets.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_media_server import panes, seal, send, sessions

SID = "1a2b3c4d-5e6f-4a1b-8c2d-0123456789ab"
FIXTURE = Path(__file__).parent / "fixtures" / "gemini" / "session-2026-10-17T00-00-1a2b3c4d.jsonl"


@pytest.fixture
def chat(tmp_path, monkeypatch):
    home = tmp_path / "gemini-home"
    monkeypatch.setenv("GEMINI_CLI_HOME", str(home))
    project = home / ".gemini" / "tmp" / "proj"
    (project / "chats").mkdir(parents=True)
    (project / ".project_root").write_text("/home/x/proj")
    path = project / "chats" / FIXTURE.name
    shutil.copy(FIXTURE, path)
    return path


def test_its_folder_and_what_sealing_takes(chat):
    assert sessions.transcript_cwd(SID) == "/home/x/proj"
    tool_out = chat.parent.parent / "tool-outputs" / f"session-{SID}"
    tool_out.mkdir(parents=True)
    harness, paths = seal._transcript_paths(SID)
    assert harness == "gemini" and chat in paths and tool_out in paths


@pytest.mark.parametrize("screen,want", [
    ("⠼ Reading the README (esc to cancel, 4s)\n> Type your message or @path/to/file", "working"),
    ("Apply this change?\n● 1. Allow once\n  2. Allow for this session\n"
     "  3. No, suggest changes (esc)", "approval"),
    ("~/proj   no sandbox   gemini-2.5-pro\n>   Type your message or @path/to/file", "input"),
    ("$ ls\nfoo bar", None),
])
def test_its_pane(screen, want):
    assert panes.classify(screen, "gemini") == want


def test_its_pane_is_found_by_argv(monkeypatch):
    """The pane says `node`; the shell's child is Gemini, and its child the
    relaunched one."""
    from agent_media_core import harnesses

    procs = {"10": ["-zsh"], "11": ["node", "/x/bin/gemini"],
             "12": ["node", "--max-old-space-size=4096", "/x/bin/gemini"], "20": ["vim"]}
    kids = {"10": ["11"], "11": ["12"]}
    monkeypatch.setattr(harnesses, "_argv", lambda pid: procs.get(pid, []))
    monkeypatch.setattr(panes, "_children", lambda pid: kids.get(pid, []))
    assert panes.agent_by_argv("10") == "gemini"
    assert panes.agent_by_argv("20") == ""


def test_a_new_chat_is_not_started_in_it_yet():
    """Phase 2 of the proposal: /ask refuses it until its fresh session and
    composer are measured."""
    assert "gemini" in send.NOT_STARTED_HERE
