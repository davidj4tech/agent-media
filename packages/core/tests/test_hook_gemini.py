"""media-hook-gemini: Gemini CLI's four hooks, as Claude Code's events.

Payloads are in the shapes gemini-cli 0.64's hook runner sends (its
docs/hooks/reference.md): the common fields, plus `prompt` (BeforeAgent),
`tool_name`/`tool_input` (BeforeTool) and `prompt_response` (AfterAgent).
"""

import io
import json

import pytest

from agent_media_core import activity, harnesses
from agent_media_core.intake import hook_claude_code, hook_gemini

SID = "1a2b3c4d-5e6f-4a1b-8c2d-0123456789ab"
GEMINI = (4242, ["node", "/x/lib/node_modules/@google/gemini-cli/bundle/gemini.js"])


def event(name, **kw):
    return {"session_id": SID, "transcript_path": "", "cwd": "/home/x/proj",
            "hook_event_name": name, "timestamp": "2026-10-17T00:00:00.000Z", **kw}


@pytest.fixture
def hook(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MEDIA_HOOK_NO_DETACH", "1")
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr(hook_gemini, "gemini_process", lambda *a, **k: GEMINI)
    seen = {"prompts": [], "said": []}
    monkeypatch.setattr(hook_claude_code, "_handle_user_prompt",
                        lambda payload, record=True: seen["prompts"].append(payload) or 0)
    monkeypatch.setattr(hook_gemini, "_speak", lambda text, session: seen["said"].append((text, session)))
    return seen


def test_a_turn_is_a_prompt_steps_and_a_spoken_reply(hook, capsys):
    hook_gemini.handle(event("BeforeAgent", prompt="Run the fast tests"))
    hook_gemini.handle(event("BeforeTool", tool_name="run_shell_command",
                             tool_input={"command": "pytest -q -m fast",
                                         "description": "Run the fast tests"}))
    hook_gemini.handle(event("BeforeTool", tool_name="read_file",
                             tool_input={"file_path": "/home/x/proj/README.md"}))
    hook_gemini.handle(event("AfterAgent", prompt="Run the fast tests",
                             prompt_response="Done: **12 passed**.", stop_hook_active=False))
    assert [p["hook_event_name"] for p in hook["prompts"]] == ["UserPromptSubmit"]
    assert hook["prompts"][0]["prompt"] == "Run the fast tests"
    turn = activity.turns(SID)[-1]
    assert [s["text"] for s in turn["steps"]] == ["Run the fast tests", "Read README.md"]
    assert turn["end"] is not None
    assert hook["said"] == [("Done: **12 passed**.", SID)]
    # Nothing but JSON may reach Gemini's stdout, and here there was nothing to say.
    assert capsys.readouterr().out == ""


def test_a_reply_with_no_words_is_not_spoken(hook):
    hook_gemini.handle(event("AfterAgent", prompt="x", prompt_response="[no response text]"))
    assert hook["said"] == []


def test_the_context_note_is_gemini_json(hook, tmp_path, capsys):
    """The note the old media-hook-context entry gave, now from this hook:
    the hookSpecificOutput Gemini reads into the turn."""
    t = tmp_path / "session.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in (
        {"sessionId": SID, "projectHash": "h"},
        {"id": "g", "type": "gemini", "model": "gemini-2.5-pro",
         "tokens": {"input": 400_000, "output": 10, "total": 400_010}},
    )) + "\n")
    hook_gemini.handle(event("BeforeAgent", prompt="go on", transcript_path=str(t)))
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["hookEventName"] == "BeforeAgent" and "76% full" in out["additionalContext"]
    assert hook["prompts"][0]["prompt"] == "go on"


def test_session_start_registers_the_pane(hook):
    hook_gemini.handle(event("SessionStart", source="startup"))
    row = json.loads((harnesses.registry_dir() / "9").read_text())
    assert row == {"harness": "gemini", "session": SID, "pid": 4242, "pane": "%9"}


def test_a_scripted_run_is_not_a_conversation(hook, monkeypatch):
    monkeypatch.setattr(hook_gemini, "gemini_process",
                        lambda *a, **k: (4242, ["node", "/x/gemini", "-p", "summarise this"]))
    hook_gemini.handle(event("BeforeAgent", prompt="summarise this"))
    hook_gemini.handle(event("AfterAgent", prompt="summarise this", prompt_response="It says hi."))
    assert hook["prompts"] == [] and hook["said"] == [] and activity.turns(SID) == []


def test_main_reads_stdin_and_never_fails(hook, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hook_gemini.main() == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event("Notification"))))
    assert hook_gemini.main() == 0
    assert capsys.readouterr().out == ""


def test_the_gemini_above_the_hook_is_found(monkeypatch):
    """Gemini runs a hook through a shell: the hook's parent is `sh`, and
    Gemini is the shell's parent."""
    tree = {"300": ("200", ["sh", "-c", "media-hook-gemini"]),
            "200": ("100", ["node", "/x/bin/gemini"]),
            "100": ("1", ["node", "/x/bin/gemini"])}
    monkeypatch.setattr(harnesses, "_argv", lambda pid: tree.get(pid, ("", []))[1])
    monkeypatch.setattr(hook_gemini, "_parent", lambda pid: int(tree.get(str(pid), ("0",))[0]))
    assert hook_gemini.gemini_process(300) == (200, ["node", "/x/bin/gemini"])
    assert hook_gemini.gemini_process(1) == (0, [])
