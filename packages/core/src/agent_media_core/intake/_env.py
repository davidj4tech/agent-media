"""Shared env-file loader for intake hooks."""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def load_env_file(label: str = "hook") -> None:
    """Load ~/.config/agent-audio-relay.env (or RELAY_ENV_FILE) into os.environ.

    Missing file is a no-op. Existing env vars are never overwritten.
    Lines starting with ``export `` are stripped before parsing.
    """
    candidates = [
        os.environ.get("RELAY_ENV_FILE") or "",
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
