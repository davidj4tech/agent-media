"""Reading Codex's and pi's screens, as the canvas does Claude Code's (panes.classify)."""

from agent_media_server import panes

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
    assert panes.classify(CODEX_IDLE, "codex") == "input"
    assert panes.classify(CODEX_WORKING, "codex") == "working"
    assert panes.classify(CODEX_APPROVAL, "codex") == "approval"
    assert panes.classify("Loading...", "codex") is None


def test_pi_states():
    assert panes.classify(PI_IDLE, "pi") == "input"
    assert panes.classify(PI_WORKING, "pi") == "working"
    assert panes.classify("npm warn deprecated …", "pi") is None


def test_claude_is_unchanged():
    assert panes.classify("? for shortcuts", "claude") == "input"


def test_codex_hook_review_is_not_a_composer():
    cap = """› Ask Codex to do anything
  Hooks need review
  3 hooks are new or changed.
› 1. Review hooks
"""
    assert panes.classify(cap, "codex") == "approval"


# A phone-width tmux window truncates Claude Code's footer.
NARROW_WORKING = """✽ Gitifying… (thought for 7s)
  ⎿  Tip: Run /install-github-app
──────────────────────────────────
❯
──────────────────────────────────
  ⏵⏵ bypass permissions on  · e…
"""
NARROW_IDLE = """  ⎿  Tip: Run /install-github-app
──────────────────────────────────
❯
──────────────────────────────────
  ⏵⏵ bypass permissions on
"""


def test_a_narrow_pane_still_reads_as_working():
    assert panes.classify(NARROW_WORKING, "claude") == "working"
    assert panes.classify(NARROW_IDLE, "claude") == "input"
