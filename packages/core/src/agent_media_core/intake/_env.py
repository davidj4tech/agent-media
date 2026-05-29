"""Shared env-file loader for intake hooks."""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def load_env_file(label: str = "hook") -> None:
    """Load env file into os.environ. Tries (in order):

    1. ``$MEDIA_ENV_FILE`` or ``$RELAY_ENV_FILE`` (legacy)
    2. ``~/.config/agent-media.env`` (current)
    3. ``~/.config/agent-audio-relay.env`` (legacy, kept so hosts that
       haven't migrated still work)

    Missing files are skipped. Loads the first file that exists. Existing
    env vars are never overwritten. ``export `` prefixes are stripped.
    """
    candidates = [
        os.environ.get("MEDIA_ENV_FILE") or "",
        os.environ.get("RELAY_ENV_FILE") or "",
        str(Path.home() / ".config" / "agent-media.env"),
        str(Path.home() / ".config" / "agent-audio-relay.env"),
    ]
    for path in candidates:
        if not path:
            continue
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
            return
        except FileNotFoundError:
            continue
        except OSError as e:
            log.warning("%s: failed to read %s: %s", label, path, e)
            return
