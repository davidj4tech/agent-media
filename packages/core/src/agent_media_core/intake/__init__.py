"""Intake adapters. Each source (hooks, CLI, MCP, HA, Matrix) normalizes
into an Event and calls submit_event.
"""

from .submit import submit_event

__all__ = ["submit_event"]
