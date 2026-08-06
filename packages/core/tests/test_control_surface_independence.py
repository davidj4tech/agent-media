"""The pipeline must never depend on a control surface.

This is the real constraint on `packages/control-surface/` — not that it can
be removed, but that the dependency only ever points **one way**:

    control surface  ──calls──▶  media CLI / call-guard  ──▶  players
    (empv, listen.el, EMMS)
                     ◀──never───

The popup, the CLI, the MCP tools and the speech coordinator must keep working
with Emacs uninstalled, dead, or wedged. So core may not import the elisp
layer, shell out to `emacsclient`, or reach for a front-end by name.

Enforced as a test rather than a promise, because a promise cannot fail CI.
See docs/control-surface.md §8.
"""

from __future__ import annotations

from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src" / "agent_media_core"

# Tokens that would mean core had acquired a front-end dependency. Deliberately
# specific: the bare phrase "control surface" is used generically in existing
# comments (e.g. sinks/__init__.py calls the CLI's hot commands that), so
# matching it would be noise rather than signal.
FORBIDDEN = (
    "emacsclient",
    "am-control",
    "am-adapter",
    "empv",
)


def _python_sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py"))


def test_sources_exist():
    """Guard the guard: a bad SRC path would make every check below vacuous."""
    files = _python_sources()
    assert len(files) > 20, f"expected the core package, found {len(files)} files"


@pytest.mark.parametrize("token", FORBIDDEN)
def test_core_does_not_reference_a_control_surface(token):
    hits = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if token in line.lower():
                hits.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")
    assert not hits, (
        f"core references {token!r} — the pipeline must not depend on a "
        "control surface; the dependency points one way only:\n  "
        + "\n  ".join(hits)
    )


def test_popup_redraw_does_not_shell_to_emacs():
    """The popup's hot path is the sharpest case: it redraws on every keypress
    and must never wait on an Emacs round-trip."""
    cli = (SRC / "cli.py").read_text(encoding="utf-8", errors="replace")
    start = cli.index("def cmd_popup_status")
    body = cli[start:start + 8000].lower()
    for token in ("emacs", "elisp"):
        assert token not in body, f"popup redraw path references {token!r}"
