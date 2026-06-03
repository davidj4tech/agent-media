"""Shared env-file loader for intake hooks."""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# Universal defaults shipped inside the package (agent_media_core/defaults.env).
# Lowest-precedence layer: fills anything the real env and machine-local files
# leave unset, so a fresh checkout has sane per-engine voices with no config.
_PACKAGE_DEFAULTS = Path(__file__).resolve().parent.parent / "defaults.env"


def _load_one(path: str, label: str) -> None:
    """Merge a single env file into os.environ without overwriting.

    Existing vars (real env or an earlier, higher-precedence file) are never
    clobbered, so calling this for each candidate from highest to lowest
    precedence layers them correctly. Missing files are skipped silently.
    """
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except FileNotFoundError:
        return
    except OSError as e:
        log.warning("%s: failed to read %s: %s", label, path, e)


def load_env_file(label: str = "hook") -> None:
    """Layer env files into os.environ, highest precedence first.

    Order (each only fills vars still unset):

    1. ``$MEDIA_ENV_FILE`` or ``$RELAY_ENV_FILE`` (legacy) — explicit override
    2. ``~/.config/agent-media.env`` — machine-local
    3. ``~/.config/agent-audio-relay.env`` — legacy machine-local
    4. ``<package>/defaults.env`` — shipped universal defaults

    Real env vars set before this runs always win (never overwritten). Unlike
    the old first-file-wins behaviour, every existing file is read so the
    shipped defaults can backfill a partial machine-local config.
    """
    candidates = [
        os.environ.get("MEDIA_ENV_FILE") or "",
        os.environ.get("RELAY_ENV_FILE") or "",
        str(Path.home() / ".config" / "agent-media.env"),
        str(Path.home() / ".config" / "agent-audio-relay.env"),
        str(_PACKAGE_DEFAULTS),
    ]
    for path in candidates:
        if not path:
            continue
        _load_one(path, label)
