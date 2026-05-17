"""Shared XDG-ish path helpers.

On Termux+proot, the proot-side `/home/<user>` is bind-mounted to the
Termux-native `/data/data/com.termux/files/home`, but services running
outside the proot (runit, MCP HTTP) only see the Termux-native path.
A second StateStore opened from native HOME diverges from the proot
one, breaking dedup and history queries across environments.

These helpers force the Termux-native HOME when it's present so all
clients share one state.db / stamp dir / audio dir.
"""

from __future__ import annotations

import os
from pathlib import Path


_TERMUX_NATIVE_HOME = Path("/data/data/com.termux/files/home")


def canonical_home() -> Path:
    """The HOME both proot and native processes should resolve to.

    Falls back to `Path.home()` off-Termux.
    """
    if _TERMUX_NATIVE_HOME.is_dir():
        return _TERMUX_NATIVE_HOME
    return Path.home()


def state_dir() -> Path:
    """`XDG_STATE_HOME/agent-media` — canonical across proot/native."""
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else canonical_home() / ".local" / "state"
    return root / "agent-media"


def cache_dir() -> Path:
    """`XDG_CACHE_HOME/agent-media` — canonical across proot/native."""
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else canonical_home() / ".cache"
    return root / "agent-media"
