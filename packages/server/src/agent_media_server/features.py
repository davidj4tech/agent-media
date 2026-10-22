"""What this server has of the optional parts, for the app (server-contract.md
§6.11a `GET /features`).

The app stays generic: it shows the Organiser, the Media tab, Contacts,
Bridges and Personas only where the server has them, so a plain install
(Claude Code, the speech hooks, nothing else) is not shown someone else's
setup (David, 23 Oct 2026: a first-time user saw his organiser, music and
bridges on a server that had none of them). Each answer is a cheap local
look — a file, a directory, an environment variable, one history row —
never a network call, so the app can ask on every start.

  org       the notes tree is there: its capture file exists (what
            /org/setup's `org` row calls ok)
  music     the house speakers have a music stream, or anything was ever
            played as music here (the play history)
  contacts  the contacts store exists, or a Google client is set up for it
  bridges   a Matrix bridge config was found (bridges.discover)
  personas  the personas repo is there (personas.repo_dir)

A check that raises answers false: the app hides that part, which is what a
server without it would show.
"""

from __future__ import annotations

import sys

from . import auth


def _org() -> bool:
    from . import org

    root = org.root()
    return root.is_dir() and (root / org.profile().capture_file).is_file()


def _music() -> bool:
    from agent_media_core import music_rooms
    from agent_media_core.state import StateStore

    if music_rooms.stream():
        return True
    return bool(StateStore().recent_history(sink="music", limit=1))


def _contacts() -> bool:
    from . import contacts

    return contacts.db_path().is_file() or contacts.client_path().is_file()


def _bridges() -> bool:
    from . import bridges

    return bool(bridges.discover())


def _personas() -> bool:
    from . import personas

    return personas.repo_dir().is_dir()


CHECKS = {"org": _org, "music": _music, "contacts": _contacts,
          "bridges": _bridges, "personas": _personas}


def detect() -> dict[str, bool]:
    out = {}
    for name, fn in CHECKS.items():
        try:
            out[name] = bool(fn())
        except Exception as e:  # noqa: BLE001 — a part we cannot see is a part not shown
            print(f"features: {name}: {e}", file=sys.stderr)
            out[name] = False
    return out


def features(bearer: str) -> tuple[bool, dict]:
    """`GET /features`, gated like `/dashboard`."""
    ok, detail = auth.may_control_speech(bearer)
    if not ok:
        return False, detail
    return True, detect()
