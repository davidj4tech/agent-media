"""Intake adapters. Each source (hooks, CLI, MCP, HA, Matrix) normalizes
into an Event and calls submit_event.

This module is also the stable public surface that out-of-tree intake packages
(agent-media-intake-matrix/ha/codex) build on, so they don't reach into
underscored modules: `submit_event`, `strip_markdown`, `run_hook_stdin`.
"""

from .submit import submit_event
from ._text import strip_markdown
from ._hook_stdin import run as run_hook_stdin

__all__ = ["submit_event", "strip_markdown", "run_hook_stdin"]
