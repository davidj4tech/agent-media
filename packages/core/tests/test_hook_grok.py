"""media-hook-grok: Grok's hooks, as Claude Code's events.

Payloads are in the shapes Grok 1.0.62's hooks guide (docs/user-guide/
10-hooks.md) gives: camelCase keys with Claude's snake_case aliases beside
them, `hook_event_name` the PascalCase name, Stop's `reason` and
`lastAssistantMessage`. None has been seen from a real Grok yet.
"""

import io
import json
import shutil
from pathlib import Path

import pytest

from agent_media_core import activity, harnesses
from agent_media_core.intake import context, hook_claude_code, hook_grok

SID = "0199f0a1-2b3c-7d4e-8f50-6172839405a6"
GROK = (4242, ["/home/x/.grok/bin/grok"])
FIXTURE = Path(__file__).parent / "fixtures" / "grok"


def event(name, **kw):
    snake = {"SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit",
             "PreToolUse": "pre_tool_use", "PostToolUse": "post_tool_use", "Stop": "stop"}
    return {"hookEventName": snake.get(name, name.lower()), "hook_event_name": name,
            "sessionId": SID, "session_id": SID, "cwd": "/home/x/proj",
            "workspaceRoot": "/home/x/proj", "timestamp": "2026-10-17T00:00:00Z",
            "permissionMode": "default", **kw}


@pytest.fixture
def hook(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MEDIA_HOOK_NO_DETACH", "1")
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr(hook_grok, "grok_process", lambda *a, **k: GROK)
    seen = {"prompts": [], "said": []}
    monkeypatch.setattr(hook_claude_code, "_handle_user_prompt",
                        lambda payload, record=True: seen["prompts"].append(payload) or 0)
    monkeypatch.setattr(hook_grok, "_speak", lambda text, session: seen["said"].append((text, session)))
    return seen


def test_a_turn_is_a_prompt_steps_and_a_spoken_reply(hook, capsys):
    hook_grok.handle(event("UserPromptSubmit", prompt="Run the fast tests"))
    hook_grok.handle(event("PreToolUse", toolName="run_terminal_command",
                           tool_name="run_terminal_command", toolUseId="call_1",
                           toolInput={"command": "pytest -q -m fast"},
                           tool_input={"command": "pytest -q -m fast"}))
    hook_grok.handle(event("PreToolUse", toolName="read_file",
                           toolInput={"path": "/home/x/proj/README.md"}))
    hook_grok.handle(event("Stop", reason="end_turn", stopHookActive=False,
                           lastAssistantMessage="Done: **12 passed**.",
                           last_assistant_message="Done: **12 passed**."))
    assert [p["hook_event_name"] for p in hook["prompts"]] == ["UserPromptSubmit"]
    assert hook["prompts"][0]["prompt"] == "Run the fast tests"
    turn = activity.turns(SID)[-1]
    # The camelCase-only event is read as well as the aliased one.
    assert [s["text"] for s in turn["steps"]] == ["Run the tests", "Read README.md"]
    assert turn["end"] is not None
    assert hook["said"] == [("Done: **12 passed**.", SID)]
    assert capsys.readouterr().out == ""


def test_the_session_end_stop_is_not_a_reply(hook):
    """Grok fires another Stop when the session ends; only end_turn speaks."""
    hook_grok.handle(event("UserPromptSubmit", prompt="hi"))
    for reason in ("channel_closed", "shutdown"):
        hook_grok.handle(event("Stop", reason=reason, lastAssistantMessage="Hello again."))
    assert hook["said"] == [] and activity.turns(SID)[-1]["end"] is None


def test_a_subagents_events_are_its_parents_business(hook):
    hook_grok.handle(event("Stop", reason="end_turn", subagentType="explore",
                           lastAssistantMessage="Found 3 files."))
    assert hook["said"] == [] and activity.turns(SID) == []


def test_the_stop_hook_answers_before_the_reply_is_spoken(hook, monkeypatch):
    """Grok holds the turn on a Stop hook: speaking goes to a detached child."""
    ran = []
    monkeypatch.setattr(hook_grok, "_detached", lambda fn: ran.append(fn))
    hook_grok.handle(event("Stop", reason="end_turn", lastAssistantMessage="Done."))
    assert hook["said"] == [] and len(ran) == 1
    ran[0]()
    assert hook["said"] == [("Done.", SID)]


def test_the_context_note_is_grok_post_tool_use_json(hook, tmp_path, monkeypatch, capsys):
    """Counted against Grok's compaction point: 80% of the model's window
    from its model list."""
    home = tmp_path / "grok-home"
    monkeypatch.setenv("GROK_HOME", str(home))
    home.mkdir(parents=True, exist_ok=True)
    (home / "models_cache.json").write_text(json.dumps({"models": {"grok-4.7": {"info": {
        "id": "grok-4.7", "context_window": 256000, "auto_compact_threshold_percent": 80}}}}))
    folder = tmp_path / "s" / SID
    folder.mkdir(parents=True)
    shutil.copy(FIXTURE / "summary.json", folder / "summary.json")
    (folder / "updates.jsonl").write_text(json.dumps({"sessionId": SID, "update": {
        "sessionUpdate": "usage_update", "used": 160_000, "size": 256_000}}) + "\n")
    hook_grok.handle(event("PostToolUse", toolName="read_file", toolResult={},
                           transcriptPath=str(folder / "updates.jsonl"),
                           transcript_path=str(folder / "updates.jsonl")))
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse"
    assert "78% full (160k of 204k)" in out["additionalContext"]
    # Said once per band, not on every tool call.
    hook_grok.handle(event("PostToolUse", transcript_path=str(folder / "updates.jsonl")))
    assert capsys.readouterr().out == ""


def test_the_compaction_point_follows_its_config(tmp_path, monkeypatch):
    home = tmp_path / "g"
    monkeypatch.setenv("GROK_HOME", str(home))
    assert context.grok_compaction_point("grok-4.7") == 204_800      # nothing on disk
    home.mkdir()
    (home / "config.toml").write_text('[model."grok-4.7"]\nauto_compact_threshold_percent = 50\n')
    assert context.grok_compaction_point("grok-4.7") == 128_000
    assert context.grok_compaction_point("grok-4.7", 500_000) == 250_000


def test_usage_from_an_anthropic_shaped_count_or_signals(tmp_path):
    folder = tmp_path / SID
    folder.mkdir()
    log = folder / "updates.jsonl"
    log.write_text(json.dumps({"update": {"sessionUpdate": "turn_usage", "usage": {
        "input_tokens": 1000, "cache_read_input_tokens": 99_000}}}) + "\n")
    assert context.grok_usage(str(log))[0] == 100_000
    log.write_text("")
    (folder / "signals.json").write_text(json.dumps({"tokens": {"context_tokens": 5000}}))
    assert context.grok_usage(str(log))[0] == 5000
    (folder / "signals.json").unlink()
    assert context.grok_usage(str(log)) is None


def test_session_start_registers_the_pane(hook):
    hook_grok.handle(event("SessionStart", source="new"))
    row = json.loads((harnesses.registry_dir() / "9").read_text())
    assert row == {"harness": "grok", "session": SID, "pid": 4242, "pane": "%9"}


def test_a_scripted_run_is_not_a_conversation(hook, monkeypatch):
    monkeypatch.setattr(hook_grok, "grok_process",
                        lambda *a, **k: (4242, ["grok", "-p", "summarise this"]))
    hook_grok.handle(event("UserPromptSubmit", prompt="summarise this"))
    hook_grok.handle(event("Stop", reason="end_turn", lastAssistantMessage="It says hi."))
    assert hook["prompts"] == [] and hook["said"] == [] and activity.turns(SID) == []


def test_main_reads_stdin_and_never_fails(hook, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hook_grok.main() == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event("Notification"))))
    assert hook_grok.main() == 0
    assert capsys.readouterr().out == ""


def test_the_grok_above_the_hook_is_found(monkeypatch):
    tree = {"300": ("200", ["sh", "-c", "media-hook-grok"]),
            "200": ("100", ["/home/x/.grok/bin/grok", "--resume", SID]),
            "100": ("1", ["-zsh"])}
    monkeypatch.setattr(harnesses, "_argv", lambda pid: tree.get(pid, ("", []))[1])
    monkeypatch.setattr(hook_grok, "_parent", lambda pid: int(tree.get(str(pid), ("0",))[0]))
    assert hook_grok.grok_process(300) == (200, ["/home/x/.grok/bin/grok", "--resume", SID])
    assert hook_grok.grok_process(1) == (0, [])
