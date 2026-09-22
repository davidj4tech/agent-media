"""Where each channel plays, chosen at runtime rather than in the env file.

Speech goes wherever ``MEDIA_SPEECH_DEFAULT_TARGET`` says, and that line
belongs to ``~/.config/agent-media.env`` — which `media-lane` rewrites as the
phone changes networks, and which nobody holding a phone can edit. This is the
one place a listener's own choice is kept:

    <state_dir>/speech-target   one target name, e.g. ``rooms``
    <state_dir>/music-where     one ``--where`` value for the next music play

Present and valid, the speech file wins over the env default everywhere a NEW
reply's target is resolved (`speech_default`). It never moves a reply that is
already speaking: a reply resolves its target once, at the start, and its
now-playing row carries that name to every pause, skip and replay aimed at it
until it ends. So a change made mid-reply is heard from the next reply on.

Absent, unreadable, or naming a target this host cannot play — the file is
ignored and the env default stands, exactly as if it did not exist. A config
change that takes a target away therefore cannot leave speech pointed at
nothing; it falls back to the lane `media-lane` chose.

The music file is narrower on purpose: it is the ``--where`` that
`media music play` uses when it is given none (``default``), and nothing else.
It never moves music that is playing, and it does not reach the MCP
`music_play` tool, whose target resolution goes through the router's own
default. See docs/server-contract.md §6.9.

"Available" is decided from configuration and local files only — the env
keys the sinks themselves read, whether a local broker's socket exists, and
the mpv breaker's shared ledger of endpoints that were just slow. Nothing
here opens a connection, so the server can answer from it on every poll.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Optional

from ._paths import state_dir

SPEECH_FILE = "speech-target"
MUSIC_FILE = "music-where"
#: Where the last `media music play` sent its track, and which track — so
#: "where is music playing" can be answered without asking a player.
MUSIC_LAST_FILE = "music-where-last"

#: The speech targets the code knows by name, in the order a picker lists
#: them. Any other name is offered only when the env configures it.
KNOWN_SPEECH = ("app", "phone", "rooms", "local")

_SPEECH_LABELS = {
    "app": "Phone (Sasonica)",
    # Sasonica Next's own Media3 player, on its own port while the two run
    # side by side. Offered only where the env configures it.
    "next": "Phone (Sasonica Next)",
    "phone": "Phone (Termux player)",
    "rooms": "House speakers",
}

#: `media music play --where` values a preference may hold. `default` is the
#: absence of one, and `local` is `rooms` under another name.
MUSIC_WHERE = ("auto", "rooms", "phone", "app")

_MUSIC_LABELS = {
    "auto": "Automatic",
    "rooms": "House speakers",
    "phone": "Phone (Termux player)",
    "app": "Phone (Sasonica)",
}

_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_PER_TARGET_PREFIXES = ("MEDIA_SPEECH_SOCKET_", "MEDIA_SPEECH_DEVICE_",
                        "MEDIA_REMOTE_SAY_CMD_")


def _path(name: str) -> Path:
    return state_dir() / name


def _read(name: str) -> str:
    """The file's first line, stripped; "" for absent or unreadable."""
    try:
        return _path(name).read_text().strip().splitlines()[0].strip()
    except (OSError, IndexError, UnicodeDecodeError):
        return ""


def _write(name: str, value: Optional[str]) -> None:
    """Replace the file atomically, or remove it for None."""
    path = _path(name)
    if value is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(value + "\n")
    os.replace(tmp, path)


# --- speech ---------------------------------------------------------------------

def env_speech_default() -> str:
    """What the env file (or media-lane) says speech defaults to."""
    return os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET") or "local"


def _host() -> str:
    try:
        return socket.gethostname().split(".")[0] or "this host"
    except OSError:
        return "this host"


def speech_label(name: str) -> str:
    if name == "local":
        return _host()
    return _SPEECH_LABELS.get(name, name)


def _configured_names() -> list[str]:
    """Target names the env mentions by a per-target key, lowercased the way
    `sinks.speech._env_key` upper-cases them."""
    found: set[str] = set()
    for key, value in os.environ.items():
        for prefix in _PER_TARGET_PREFIXES:
            if key.startswith(prefix) and len(key) > len(prefix):
                name = key[len(prefix):].lower().replace("_", "-")
                # MEDIA_REMOTE_SAY_CMD_ROOMS= (the `-` sentinel, emptied by
                # the env loader) says "not by the lane", not "a target".
                if _NAME.fullmatch(name) and (value or prefix != "MEDIA_REMOTE_SAY_CMD_"):
                    found.add(name)
    return sorted(found)


def speech_candidates() -> list[str]:
    """Every speech target this host might offer: the known four, whatever
    the env configures per target, and the env default itself."""
    names = list(KNOWN_SPEECH)
    for n in _configured_names() + [env_speech_default()]:
        if n not in names:
            names.append(n)
    return names


def _lane(name: str) -> str:
    """The remote-say command for this target, as submit resolves it."""
    from .sinks.speech import _env_key

    per = os.environ.get(_env_key("MEDIA_REMOTE_SAY_CMD", name))
    return per if per is not None else os.environ.get("MEDIA_REMOTE_SAY_CMD", "")


def speech_configured(name: str) -> tuple[bool, Optional[str]]:
    """Whether a reply sent to `name` on this host has anywhere to go.

    The same two routes submit takes: a remote-say lane for the target (the
    words are rendered elsewhere), else a local render played through the
    speech broker, which needs `sinks.speech._device_for` to know the target.
    `(False, why)` otherwise — the name is unknown, or nothing is set for it.
    """
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return False, "not a target name"
    if name not in speech_candidates():
        return False, f"unknown target {name!r}"
    if _lane(name):
        return True, None
    from .sinks.speech import _device_for
    from .types import Target

    try:
        _device_for(Target(name=name))
    except NotImplementedError:
        return False, "not configured on this host"
    return True, None


def speech_override() -> Optional[str]:
    """The listener's choice, when there is one and this host can play it."""
    name = _read(SPEECH_FILE)
    if not name:
        return None
    try:
        ok, _why = speech_configured(name)
    except Exception:  # noqa: BLE001 — a bad override must never stop speech
        return None
    return name if ok else None


def speech_default() -> str:
    """Where the next reply plays: the override, else the env default.

    Every place that resolves a NEW reply's target calls this. It never
    raises — whatever goes wrong reading the override, the answer is the env
    default, which is what the code said before this existed.
    """
    try:
        return speech_override() or env_speech_default()
    except Exception:  # noqa: BLE001
        return env_speech_default()


def set_speech_override(name: Optional[str]) -> Optional[str]:
    """Set (a name) or clear (None / "") the speech override.

    Raises ValueError naming why a target was refused. Returns what was set.
    """
    name = (name or "").strip().lower()
    if not name:
        _write(SPEECH_FILE, None)
        return None
    ok, why = speech_configured(name)
    if not ok:
        raise ValueError(why or f"unknown target {name!r}")
    _write(SPEECH_FILE, name)
    return name


def _speech_reachability(name: str) -> tuple[bool, Optional[str]]:
    """Cheap local signs that the player behind `name` is (not) there.

    A local broker's socket file is either present or not. A bridge
    (tcp://) is not probed: only the mpv breaker's shared ledger is read,
    which says whether the last call to it was slow or failed.
    """
    if _lane(name):
        return True, None
    from .sinks.speech import _socket_for
    from .types import Target

    sock = str(_socket_for(Target(name=name)))
    if sock.startswith("tcp://"):
        try:
            from . import _breaker

            if sock in _breaker.load("mpv"):
                return True, "was slow or unreachable a moment ago"
        except Exception:  # noqa: BLE001
            pass
        return True, None
    if not Path(sock).exists():
        return False, f"no speech player running on {_host()}"
    return True, None


def speech_options() -> list[dict]:
    """One row per target this host knows: `{name, label, available, why}`.

    Only configured targets are listed, plus the env default and the
    override whatever their state, so the picker always shows what is in
    force. Unconfigured known targets are left out rather than shown dead:
    a house without speakers has no `rooms` to offer.
    """
    keep = {env_speech_default(), _read(SPEECH_FILE)}
    out = []
    for name in speech_candidates():
        ok, why = speech_configured(name)
        if not ok and name not in keep:
            continue
        if ok:
            ok, why = _speech_reachability(name)
        out.append({"name": name, "label": speech_label(name),
                    "available": ok, "why": why})
    return out


def speech_block() -> dict:
    """The `speech` channel of `GET /audio/targets`."""
    override = speech_override()
    return {"current": override or env_speech_default(),
            "default": env_speech_default(),
            "overridden": override is not None,
            "options": speech_options()}


# --- music ----------------------------------------------------------------------

def music_available(where: str) -> tuple[bool, Optional[str]]:
    if where not in MUSIC_WHERE:
        return False, f"unknown place {where!r}"
    if where == "app":
        from .sinks.music_app import configured
        if not configured():
            return False, "Sasonica's music player is not configured here"
    if where == "phone":
        from .sinks.music_local import configured
        if not configured():
            return False, "the phone's music player is not configured here"
    return True, None


def music_pref() -> Optional[str]:
    """The `--where` the next untargeted `media music play` uses, if set."""
    where = _read(MUSIC_FILE)
    return where if where in MUSIC_WHERE else None


def set_music_pref(where: Optional[str]) -> Optional[str]:
    where = (where or "").strip().lower()
    if where in ("", "default"):
        _write(MUSIC_FILE, None)
        return None
    if where == "local":
        where = "rooms"
    ok, why = music_available(where)
    if not ok:
        raise ValueError(why or f"unknown place {where!r}")
    _write(MUSIC_FILE, where)
    return where


def note_music_played(where: str, uri: str) -> None:
    """Remember where `media music play` sent `uri`. Best-effort."""
    try:
        _write(MUSIC_LAST_FILE, json.dumps({"where": where, "uri": uri,
                                            "at": round(time.time(), 3)}))
    except OSError:
        pass


def music_now(intent: Optional[dict]) -> Optional[str]:
    """Where music is playing, if that is known without asking a player.

    Known only when the channel's intent (what it was last asked to play,
    cleared on stop) is the very track this host last sent somewhere. A play
    this host did not route — the MCP tool, the popup on another host — is
    `None`, not a guess.
    """
    if not intent or not intent.get("uri"):
        return None
    try:
        last = json.loads(_path(MUSIC_LAST_FILE).read_text())
    except (OSError, ValueError):
        return None
    if isinstance(last, dict) and last.get("uri") == intent.get("uri"):
        where = last.get("where")
        return where if where in MUSIC_WHERE else None
    return None


def music_options() -> list[dict]:
    out = []
    for where in MUSIC_WHERE:
        ok, why = music_available(where)
        out.append({"name": where, "label": _MUSIC_LABELS[where],
                    "available": ok, "why": why})
    return out


def music_block(intent: Optional[dict] = None) -> dict:
    """The `music` channel of `GET /audio/targets`."""
    pref = music_pref()
    return {"current": music_now(intent),
            "next": pref or "default",
            "overridden": pref is not None,
            "options": music_options()}
