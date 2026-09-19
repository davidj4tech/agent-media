"""Reading Codex's and pi's screens, as the canvas does Claude Code's."""

from agent_media_visual import canvas

CODEX_IDLE = """  Tip: You can resume a previous conversation
› Ask Codex to do anything
  gpt-6-astra default · ~/scratch
"""
CODEX_WORKING = """› Run the shell command
• I’ll run the command now.
• Working (6s • esc to interrupt)
› Ask Codex to do anything
"""
CODEX_APPROVAL = """  Would you like to run the following command?
  $ rm -rf build
› 1. Yes, proceed (y)
"""
PI_IDLE = """ ok
──────────────────────────────────

──────────────────────────────────
~/scratch • Run the shell comma...
↑11 ↓83 R11k W12k CH93.7% $0.38...
"""
PI_WORKING = """ $ sleep 5; echo done
 ⠸ Working...
──────────────────────────────────
──────────────────────────────────
~/scratch
"""


def test_codex_states():
    assert canvas._classify_agent(CODEX_IDLE, "codex") == "input"
    assert canvas._classify_agent(CODEX_WORKING, "codex") == "working"
    assert canvas._classify_agent(CODEX_APPROVAL, "codex") == "approval"
    assert canvas._classify_agent("Loading...", "codex") is None


def test_pi_states():
    assert canvas._classify_agent(PI_IDLE, "pi") == "input"
    assert canvas._classify_agent(PI_WORKING, "pi") == "working"
    assert canvas._classify_agent("npm warn deprecated …", "pi") is None


def test_claude_is_unchanged():
    assert canvas._classify_agent("? for shortcuts", "claude") == "input"


def test_codex_hook_review_is_not_a_composer():
    cap = """› Ask Codex to do anything
  Hooks need review
  3 hooks are new or changed.
› 1. Review hooks
"""
    assert canvas._classify_agent(cap, "codex") == "approval"
