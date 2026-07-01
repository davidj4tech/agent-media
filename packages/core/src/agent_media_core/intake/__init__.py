"""Intake adapters. Each source (hooks, CLI, MCP, HA, Matrix) normalizes
into an Event and calls submit_event.

This module is also the stable public surface that out-of-tree intake packages
(agent-media-intake-matrix/ha/codex) build on, so they don't reach into
underscored modules: `submit_event`, `strip_markdown`, `run_hook_stdin`.
"""

__all__ = ["submit_event", "strip_markdown", "run_hook_stdin"]


def __getattr__(name: str):
    # `submit` pulls in the whole render/highlight stack (~11ms). The CLI reaches
    # it only for `media say`, and imports it lazily inside those handlers; but
    # importing `.intake._env` (which the CLI does at startup for config) would
    # otherwise drag `submit` in through this package init. Defer it (PEP 562) so
    # `from .intake._env import load_env_file` stays cheap, while the public
    # `from agent_media_core.intake import submit_event` still works.
    if name == "submit_event":
        from .submit import submit_event
        return submit_event
    if name == "strip_markdown":
        from ._text import strip_markdown
        return strip_markdown
    if name == "run_hook_stdin":
        from ._hook_stdin import run as run_hook_stdin
        return run_hook_stdin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
