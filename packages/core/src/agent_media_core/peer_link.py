"""Reaching another host's server: its URL and this host's device token for it.

A host asks another host's gated routes (`POST /services/restart`, …) as a
paired device of that host's server, the same credential the Sasonica app and
the phone's Termux worker (deploy/phone/service/phone-jobs) hold: server
contract §9. No ssh, and no scheme of its own.

Pairing, once per pair of hosts:

* on the host to be reached, mint a device code:
  `media-visual-canvas pair --device "<this host> media"` (or from an enrolled
  app: Settings, Devices). It prints a `sasonica://pair?server=…&code=…` link.
* here, `media peer pair NAME '<that link>'` (or the bare code with
  `--server URL`). It trades the code at `POST /pair` and keeps the token,
  and the URL it reached, in `~/.config/agent-media/peers/<alias>.json`
  (0600).

NAME is a peer's alias in config.toml's `[peers.*]` (`phone`), its `host`
(`p8a`), or a role (`peer()`'s fallback); with no peers table it is just a
name. The alias is the key, so `--host p8a` and `--host phone` are one link.
Without a link URL the default is `http://<host>:<MEDIA_VISUAL_PORT or 8781>`.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from . import config

#: Cloudflare turns away Python's own "Python-urllib/x.y" (error 1010) when a
#: peer's server is behind a tunnel, so the client names itself.
UA = "agent-media-peer/1"


class PeerError(RuntimeError):
    pass


def store_dir() -> Path:
    return config.config_path().parent / "peers"


def resolve(name: str) -> "tuple[str, Optional[config.Peer]]":
    """`(alias, peer)` for NAME: an alias, a peer's host, else a role."""
    name = name.strip()
    found = config.peers()
    if name in found:
        return name, found[name]
    for alias in sorted(found):
        if found[alias].host.split(".")[0].lower() == name.split(".")[0].lower():
            return alias, found[alias]
    p = config.peer(name)
    if p:
        return p.alias, p
    return name, None


def _file(alias: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in alias) or "peer"
    return store_dir() / f"{safe}.json"


def load(name: str) -> "Optional[dict]":
    """The stored link for NAME ({"alias", "url", "token", …}), or None."""
    alias, _ = resolve(name)
    try:
        got = json.loads(_file(alias).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(got, dict) or not got.get("token") or not got.get("url"):
        return None
    got.setdefault("alias", alias)
    return got


def default_url(peer: "Optional[config.Peer]") -> str:
    if not peer:
        return ""
    port = os.environ.get("MEDIA_VISUAL_PORT") or "8781"
    return f"http://{peer.host}:{port}"


def parse_link(link: str) -> "tuple[str, str]":
    """`(server, code)` from a `sasonica://pair?server=…&code=…` link, or a
    `…/pair?c=…` one; a bare code is `("", code)`."""
    link = link.strip()
    if "?" not in link:
        return "", link
    qs = urllib.parse.parse_qs(link.partition("?")[2])
    code = (qs.get("code") or qs.get("c") or [""])[0]
    server = (qs.get("server") or [""])[0]
    if not server and link.startswith(("http://", "https://")):
        u = urllib.parse.urlsplit(link)
        server = f"{u.scheme}://{u.netloc}"
    return server.rstrip("/"), code


def _http(url: str, body: "Optional[dict]", token: str = "", timeout: float = 30.0,
          method: str = "POST") -> "tuple[int, dict]":
    headers = {"User-Agent": UA, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (OSError, ValueError) as e:
        raise PeerError(f"could not reach {url}: {e}") from e
    try:
        got = json.loads(raw or b"{}")
    except ValueError:
        got = {"error": raw[:200].decode("utf-8", "replace")}
    return status, got if isinstance(got, dict) else {"answer": got}


def pair(name: str, link: str, server: str = "") -> dict:
    """Trade a device pairing code from NAME's server for a token, and keep it.
    Returns the stored link (the token included)."""
    alias, peer = resolve(name)
    link_server, code = parse_link(link)
    base = (server or link_server or default_url(peer)).rstrip("/")
    if not code:
        raise PeerError("no pairing code in that link")
    if not base:
        raise PeerError(f"no URL for {name!r}: not a configured peer and the link "
                        "names no server; pass --server URL")
    me = socket.gethostname().split(".")[0]
    status, got = _http(base + "/pair", {"code": code, "device": f"{me} media"})
    if status != 200 or not got.get("token"):
        raise PeerError(f"{base}/pair said {status}: {got.get('error') or got}")
    rec = {"alias": alias, "url": base, "token": got["token"],
           "device_id": got.get("device_id"), "name": got.get("name"),
           "server_name": (got.get("server") or {}).get("name"),
           "paired_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    path = _file(alias)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(rec, fh, indent=2)
    os.replace(tmp, path)
    return rec


def links() -> list[dict]:
    """Every configured peer and stored link: {"alias", "host", "url",
    "paired"}; tokens left out."""
    out, seen = [], set()
    for alias, p in sorted(config.peers().items()):
        got = load(alias)
        out.append({"alias": alias, "host": p.host,
                    "url": (got or {}).get("url") or default_url(p), "paired": bool(got)})
        seen.add(alias)
    d = store_dir()
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        got = load(f.stem)
        if got and got["alias"] not in seen:
            out.append({"alias": got["alias"], "host": "", "url": got["url"],
                        "paired": True})
    return out


def request(name: str, path: str, body: "Optional[dict]" = None, *,
            timeout: float = 30.0, method: str = "POST") -> "tuple[int, dict, dict]":
    """`(status, answer, link)` for `method path` on NAME's server, as this
    host's device there. PeerError when there is no link or no answer."""
    got = load(name)
    if not got:
        alias, peer = resolve(name)
        where = f" (its server: {default_url(peer)})" if peer else ""
        raise PeerError(
            f"this host is not paired with {name!r}'s server{where}. On {name}, run "
            f"`media-visual-canvas pair --device \"{socket.gethostname().split('.')[0]} "
            f"media\"`, then here `media peer pair {alias} '<the sasonica://pair link>'`")
    status, ans = _http(got["url"].rstrip("/") + path, body, got["token"], timeout, method)
    return status, ans, got
