"""One file that says what this host is and who its peers are.

    ~/.config/agent-media/config.toml

    [host]
    roles = ["observe", "render"]      # what this machine can do

    [peers.hub]
    host  = "red5"
    roles = ["render", "origin"]

    [peers.speaker]
    host  = "sp4"
    roles = ["render"]

## Why a file and not more environment variables

The env file works and everything already reads it, but it cannot express a
table. "Who else is out there, and what can each of them do" is a list of
records, and encoding that as ``MEDIA_PEER_1_HOST`` is how config files get
invented badly. So peers live here.

## Roles

The same three the service installer uses, and deliberately not more:

``observe``  a mic or a dialer worth watching
``render``   audio sinks that can be silenced
``origin``   produces the text to be spoken

A host may hold any combination. The phone is ``observe`` + ``render`` as part
of a fleet, and ``observe`` + ``render`` + ``origin`` standing alone — which is
the whole of the difference between the two, and the reason "standalone" is a
configuration here rather than a mode in the code.

## The point of aliases

Code asks for ``peer("origin")`` or ``peers_with("render")``. It never names a
machine. Hostnames appear in exactly one place, this file, which is what makes
the package installable by somebody whose machines are not called red5 and p8a.

## Precedence, and why nothing here is required

Environment beats file, for every value: a one-off ``MEDIA_ROLES=render`` must
win over what is on disk, and services already receive their environment from
the same place they always did.

Absent beats wrong. A missing file, an unreadable one, malformed TOML — all
read as "nothing configured", never as an error, because every consumer already
has a defined behaviour for "unset" and none of them has one for "the config
raised". The one thing this module must never do is stop a host making sound
because its config file has a typo.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


log = logging.getLogger(__name__)

CONFIG_ENV = "MEDIA_CONFIG"
ROLES_ENV = "MEDIA_ROLES"

LEGACY_ROLES_NAME = "agent-media-roles"

KNOWN_ROLES = ("observe", "render", "origin")


def legacy_roles_path() -> Path:
    """The pre-TOML plain roles file. Still read, still honoured.

    A function, not a module constant. Resolved at import time it would freeze
    whatever HOME happened to be when the module first loaded, which is wrong
    for anything that changes HOME afterwards and silently wrong in tests --
    where it read the developer's real roles file and reported a host that can
    do everything.
    """
    return Path.home() / ".config" / LEGACY_ROLES_NAME


def config_path() -> Path:
    raw = os.environ.get(CONFIG_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".config" / "agent-media" / "config.toml"


def load(path: Path | None = None) -> dict:
    """The parsed config, or ``{}``.

    Not cached. It is a small file read at start-up by short-lived CLI calls,
    and a cache would mean a long-running service holding a stale view of a
    file the user just edited — the kind of thing that gets diagnosed as "the
    setting does not work".
    """
    p = path or config_path()
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        # Loud enough to find, quiet enough not to break a boot.
        log.warning("agent-media config: ignoring %s (%s)", p, e)
        return {}
    return data if isinstance(data, dict) else {}


def _as_roles(value) -> set[str]:
    """A role list from a TOML array or a loose string.

    Comment-stripping is not optional here. The legacy roles file is a plain
    text file that people (and the installer's own docs) write comments in, and
    without this every word of the explanation becomes a role — which reads as
    a host that can do everything, so nothing is ever filtered.
    """
    if isinstance(value, str):
        cleaned = []
        for line in value.splitlines():
            cleaned.append(line.split("#", 1)[0])
        value = " ".join(cleaned).replace(",", " ").split()
    if not isinstance(value, (list, tuple)):
        return set()
    return {str(v).strip().lower() for v in value if str(v).strip()}


def host_roles(path: Path | None = None) -> set[str] | None:
    """This host's roles, or None when nothing declares any.

    None is not the empty set, and the distinction is load-bearing for the
    service installer: unconfigured means "filter nothing", so a host that has
    never heard of roles installs what it always installed. An empty
    *declaration* is a declaration and does filter.

    Order: ``MEDIA_ROLES`` → ``[host] roles`` → the legacy roles file.
    """
    raw = os.environ.get(ROLES_ENV)
    if raw is not None:
        return _as_roles(raw)

    host = load(path).get("host")
    if isinstance(host, dict) and "roles" in host:
        return _as_roles(host.get("roles"))

    try:
        return _as_roles(legacy_roles_path().read_text())
    except OSError:
        return None


@dataclass(frozen=True)
class Peer:
    alias: str
    host: str
    roles: frozenset = field(default_factory=frozenset)

    def can(self, role: str) -> bool:
        return role.lower() in self.roles


def peers(path: Path | None = None) -> dict[str, Peer]:
    """``{alias: Peer}`` from ``[peers.*]``. Empty when standalone.

    An entry with no ``host`` is skipped rather than defaulted to its alias:
    guessing that the peer called "hub" is reachable at the hostname "hub"
    would produce connection attempts to a machine nobody named.
    """
    table = load(path).get("peers")
    if not isinstance(table, dict):
        return {}
    out: dict[str, Peer] = {}
    for alias, entry in table.items():
        if not isinstance(entry, dict):
            continue
        host = str(entry.get("host") or "").strip()
        if not host:
            log.warning("agent-media config: peer %r has no host — skipped",
                        alias)
            continue
        out[str(alias)] = Peer(str(alias), host,
                               frozenset(_as_roles(entry.get("roles"))))
    return out


def peer(alias: str, path: Path | None = None) -> Peer | None:
    """A peer by alias, or by role when no alias matches.

    The fallback is what lets a call site say ``peer("origin")`` without
    knowing whether the fleet calls that machine "hub" or "desktop". With more
    than one candidate the first by alias order wins, deterministically —
    arbitrary, but stable, which matters more than clever here.
    """
    found = peers(path)
    if alias in found:
        return found[alias]
    for name in sorted(found):
        if found[name].can(alias):
            return found[name]
    return None


def peers_with(role: str, path: Path | None = None) -> list[Peer]:
    """Every peer that can do `role`, in alias order."""
    found = peers(path)
    return [found[a] for a in sorted(found) if found[a].can(role)]


def peer_hosts(path: Path | None = None) -> list[str]:
    """Hostnames of every configured peer, in alias order."""
    found = peers(path)
    return [found[a].host for a in sorted(found)]
