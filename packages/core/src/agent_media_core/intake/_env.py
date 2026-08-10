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

# "I mean off", as opposed to "this happens to be empty".
#
# An empty var is backfilled from the config files (see _load_one), which is
# right for a login shell exporting OPENAI_API_KEY='' and wrong for someone
# switching a feature off for one invocation — `MEDIA_REMOTE_SAY_CMD= media
# say ...` was silently refilled from agent-media.env, so the setting could
# not be turned off at all without editing the file. Nothing can tell those
# two empties apart, so the deliberate one says so: a value of `-` is turned
# into an empty string that no later layer will fill.
_OFF = "-"

# Keys switched off, remembered for the life of the process rather than for one
# call. load_env_file runs more than once in a process — cli.py alone calls it
# at import and again in main() — and by the second call the sentinel has
# already become an empty string, which is precisely what a load backfills. A
# per-call set left the switch on for everything that ran after the second
# call, and every unit test that called the loader once passed anyway.
_OFF_KEYS: set[str] = set()

# Namespaces this loader owns. A `-` is normalised for these keys whether or
# not any file mentions them: the switch that most needs turning off is a
# per-target one like MEDIA_REMOTE_SAY_CMD_ROOMS, which by design appears in no
# file — it exists only to override the global fallback. Left unnormalised the
# literal "-" is truthy, so the setting reads as ON and something tries to run
# "-" as a command. Other keys are still handled when a file names them, so an
# unrelated var whose real value is legitimately `-` is never touched.
_OFF_PREFIXES = ("MEDIA_", "RELAY_")


def _mark_explicit_off() -> None:
    """Turn `-` into an empty string for this loader's own namespaces."""
    for k, v in list(os.environ.items()):
        if v == _OFF and k.startswith(_OFF_PREFIXES):
            os.environ[k] = ""
            _OFF_KEYS.add(k)


def _dequote(v: str) -> str:
    """Remove one matched pair of surrounding quotes, and only that.

    This was `.strip('"').strip("'")`, which removes quote characters from
    either end whether or not they pair up. A value that merely *ends* in a
    quote — a command whose last argument is quoted, say — came back with its
    closing quotes eaten and its opening ones intact, so the shell that later
    ran it died on an unterminated string. The failure surfaces far from here,
    in a command that looks correct in the file it was read from.
    """
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _load_one(path: str, label: str, off: set[str] | None = None) -> None:
    """Merge a single env file into os.environ without overwriting.

    Existing vars (real env or an earlier, higher-precedence file) are never
    clobbered, so calling this for each candidate from highest to lowest
    precedence layers them correctly. Missing files are skipped silently.

    A *present-but-empty* var (e.g. `OPENAI_API_KEY=''` exported by a login
    shell) counts as unset for layering: it would otherwise block a real value
    from the config file while contributing nothing, so it gets backfilled.

    A var set to `-` is the deliberate opposite — see _OFF. It becomes empty
    and is recorded in `off` (defaulting to the process-wide _OFF_KEYS), so
    neither a lower-precedence file nor a later call can undo the decision.
    Only keys that appear in an env file are considered, so an unrelated var
    whose real value is `-` is never touched.
    """
    if off is None:
        off = _OFF_KEYS
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
                k, v = k.strip(), _dequote(v.strip())
                if not k or k in off:
                    continue
                cur = os.environ.get(k)
                if cur == _OFF or (not cur and v == _OFF):
                    # From the environment or from the file — `-` means off
                    # either way. A config file is the more likely place to
                    # write it (`MEDIA_REMOTE_SAY_CMD_ROOMS=-` to keep one
                    # target rendering locally), and left unhandled the literal
                    # "-" is truthy, so the setting reads as ON with a command
                    # of "-" that fails when something tries to run it.
                    os.environ[k] = ""
                    off.add(k)
                    continue
                if not cur:
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

    Real env vars set before this runs always win (never overwritten), with
    one exception in each direction: an empty one is treated as unset and gets
    backfilled, and one set to `-` is turned into an empty string that stays
    empty (see _OFF — this is how a setting is switched off for a single
    invocation). Unlike the old first-file-wins behaviour, every existing file
    is read so the shipped defaults can backfill a partial machine-local
    config.
    """
    off = _OFF_KEYS                # honoured by every layer, and by later calls
    _mark_explicit_off()
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
        _load_one(path, label, off)
