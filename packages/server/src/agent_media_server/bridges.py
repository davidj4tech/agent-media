"""Bridges: signing in from the app (contract §6.26).

The Matrix rooms come from mautrix bridges on this host (Google Messages,
Meta, Instagram, Teams, …). Each speaks the bridgev2 provisioning API
(`/_matrix/provision/v3`, bearer = its `provisioning.shared_secret`,
`?user_id=` the owner's Matrix id). This relays it so the app can see each
bridge's sign-ins and sign in, sign in again or sign out without the bot
chat. The app stays generic: it draws whatever `LoginStep` a bridge sends.

* **Which bridges**: their config files (`MATRIX_BRIDGES`, comma-separated
  paths or globs; default `~/.local/share/mautrix-*/config.yaml`), read by a
  reader for exactly the keys used (`read_config`) — no YAML dependency.
  The secret never leaves the server.
* **A sign-in** is a login process held here in memory (`_PROCS`), keyed by
  the bridge's own process id. A `display_and_wait` step (a QR, a code) is
  waited on here, in a daemon thread per process: the bridge's step call
  blocks until the wait ends (up to its 30 min), so the app polls
  `GET /bridges/<id>/login/<process>` instead of holding a request open.
  A process is forgotten `PROC_TTL_S` after its last activity.
"""

from __future__ import annotations

import glob
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

DEFAULT_GLOB = "~/.local/share/mautrix-*/config.yaml"
PROV = "/_matrix/provision/v3"
#: A whoami (the listing) waits this long per bridge; they are asked at once.
WHOAMI_TIMEOUT_S = 5.0
#: Starting a flow and submitting a form can make the bridge talk to its
#: network (a password check, fetching a QR) before it answers.
STEP_TIMEOUT_S = 120.0
#: A `display_and_wait` step blocks at the bridge until the wait ends; the
#: bridge's own process lives 30 min.
WAIT_TIMEOUT_S = 20 * 60.0
#: A process is forgotten this long after its last activity.
PROC_TTL_S = 30 * 60.0
#: The states the app marks and offers Sign in again for.
NEEDS_YOU = frozenset({"BAD_CREDENTIALS", "LOGGED_OUT", "UNKNOWN_ERROR"})
#: Secrets that mean "no provisioning API" (mautrix's own sentinels).
_NO_SECRET = {"", "generate", "disable", "null", "~"}
#: Step types the app submits values for.
_SUBMITTED = ("user_input", "cookies")


class BridgeError(Exception):
    """The bridge answered with an error: its HTTP status and message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Unreachable(Exception):
    """The bridge did not answer at all."""


class Conflict(Exception):
    """A submit for a step that is no longer the current one (409)."""


# --- the config reader ------------------------------------------------------------

def _scalar(raw: str) -> str:
    """A YAML scalar's text: quotes removed (with their escapes), an inline
    comment dropped from a plain one."""
    raw = raw.strip()
    if raw[:1] == '"':
        out, i = [], 1
        while i < len(raw):
            c = raw[i]
            if c == "\\" and i + 1 < len(raw):
                nxt = raw[i + 1]
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
                i += 2
                continue
            if c == '"':
                break
            out.append(c)
            i += 1
        return "".join(out)
    if raw[:1] == "'":
        out, i = [], 1
        while i < len(raw):
            if raw[i] == "'":
                if raw[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                break
            out.append(raw[i])
            i += 1
        return "".join(out)
    if raw.startswith("#"):
        return ""
    cut = raw.find(" #")
    return (raw[:cut] if cut >= 0 else raw).strip()


def _split_key(text: str) -> tuple[str, str] | None:
    """`key: value` → (key, value text); None for a line that is not one."""
    if text[:1] in ('"', "'"):
        q = text[0]
        end = text.find(q, 1)
        if end < 0 or text[end + 1:end + 2] != ":":
            return None
        return text[1:end], text[end + 2:]
    i = text.find(":")
    while i >= 0 and i + 1 < len(text) and text[i + 1] not in " \t":
        i = text.find(":", i + 1)
    if i <= 0:
        return None
    return text[:i].strip(), text[i + 1:]


def read_config(text: str) -> dict[str, str]:
    """The block mappings of a mautrix config, flattened: `{"appservice.id":
    "gmessages", "provisioning.shared_secret": "…", …}`.

    Enough YAML for these files and no more: nested `key: value` mappings
    by indentation, quoted or plain scalars, comments. Lists and block
    scalars (`|`, `>`) are stepped over whole — no key inside one is read.
    """
    flat: dict[str, str] = {}
    stack: list[tuple[int, str]] = []    # (indent, key) of the open mappings
    skip: int | None = None              # inside a list / block scalar at this indent
    skip_list = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        if skip is not None:
            if indent > skip or (skip_list and indent == skip and body.startswith("-")):
                continue
            skip, skip_list = None, False
        if body.startswith("- ") or body == "-":
            # A list straight under the last key (YAML lets it sit at the
            # key's own indent): step over it.
            skip, skip_list = indent, True
            continue
        kv = _split_key(body)
        if kv is None:
            continue
        key, rest = kv
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([k for _, k in stack] + [key])
        value = rest.strip()
        if not value or value.startswith("#"):
            stack.append((indent, key))       # a mapping (or a list) opens
            continue
        if value[:1] in ("|", ">"):
            skip, skip_list = indent, False   # a block scalar: its lines are deeper
            continue
        flat[path] = _scalar(value)
    return flat


# --- which bridges ----------------------------------------------------------------

@dataclass(frozen=True)
class Bridge:
    id: str
    url: str          # http://host:port, where its provisioning API listens
    secret: str
    homeserver: str   # the homeserver it talks to (for the network icon)
    config: str


def _paths() -> list[str]:
    spec = os.environ.get("MATRIX_BRIDGES") or DEFAULT_GLOB
    out: list[str] = []
    for part in spec.split(","):
        part = os.path.expanduser(part.strip())
        if not part:
            continue
        hits = sorted(glob.glob(part)) if glob.has_magic(part) else [part]
        out.extend(h for h in hits if h not in out)
    return out


def discover() -> list[Bridge]:
    """Every bridge with a provisioning secret, from its config file."""
    found: dict[str, Bridge] = {}
    for p in _paths():
        try:
            conf = read_config(Path(p).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        bid = conf.get("appservice.id") or ""
        secret = conf.get("provisioning.shared_secret") or ""
        port = conf.get("appservice.port") or ""
        if not bid or secret.lower() in _NO_SECRET or not port.isdigit() or bid in found:
            continue
        host = conf.get("appservice.hostname") or "127.0.0.1"
        if host in ("0.0.0.0", "::", "[::]"):
            host = "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        found[bid] = Bridge(id=bid, url=f"http://{host}:{port}", secret=secret,
                            homeserver=(conf.get("homeserver.address") or "").rstrip("/"),
                            config=p)
    return list(found.values())


def find(bridge_id: str) -> Bridge | None:
    return next((b for b in discover() if b.id == bridge_id), None)


def _owner() -> str:
    from agent_media_core import matrix

    return matrix.owner_of()


# --- talking to a bridge ----------------------------------------------------------

def _call(b: Bridge, method: str, path: str, body: dict | None = None,
          query: dict | None = None, timeout: float = 10.0) -> dict:
    """One provisioning call: its JSON answer. BridgeError when the bridge
    refuses, Unreachable when it does not answer."""
    q = {"user_id": _owner(), **(query or {})}
    url = f"{b.url}{PROV}{path}?{urllib.parse.urlencode(q)}"
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {b.secret}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        try:
            got = json.loads(e.read() or b"{}")
        except ValueError:
            got = {}
        msg = (got.get("error") if isinstance(got, dict) else "") or f"HTTP {e.code}"
        raise BridgeError(e.code, str(msg)) from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", None) or e
        raise Unreachable(str(reason)) from None
    try:
        got = json.loads(raw or b"{}")
    except ValueError:
        raise BridgeError(502, "the bridge's answer is not JSON") from None
    return got if isinstance(got, dict) else {}


def _q(part: str) -> str:
    return urllib.parse.quote(part, safe="")


# --- the listing ------------------------------------------------------------------

#: The network icon each bridge last named (bridge id → mxc), from whoami.
_ICON_MXC: dict[str, str] = {}
#: Fetched icons (mxc → (bytes, content type)).
_ICONS: dict[str, tuple[bytes, str]] = {}
_LOCK = threading.Lock()


def _login(item: dict) -> dict:
    st = item.get("state") if isinstance(item.get("state"), dict) else {}
    event = st.get("state_event") or item.get("state_event") or None
    since = st.get("timestamp") or item.get("state_ts") or None
    return {"id": str(item.get("id") or ""), "name": str(item.get("name") or ""),
            "state": event, "error": st.get("error") or None,
            "message": st.get("message") or None,
            "since": int(since) if isinstance(since, (int, float)) else None}


def entry(b: Bridge) -> dict:
    """One bridge as `GET /bridges` lists it, asked now."""
    try:
        who = _call(b, "GET", "/whoami", timeout=WHOAMI_TIMEOUT_S)
    except (BridgeError, Unreachable) as e:
        return {"id": b.id, "name": b.id, "network": b.id, "icon": None,
                "reachable": False, "error": str(e) or type(e).__name__,
                "flows": [], "logins": []}
    net = who.get("network") if isinstance(who.get("network"), dict) else {}
    mxc = str(net.get("network_icon") or "")
    with _LOCK:
        if mxc:
            _ICON_MXC[b.id] = mxc
        else:
            _ICON_MXC.pop(b.id, None)
    flows = [{"id": str(f.get("id") or ""), "name": str(f.get("name") or ""),
              "description": str(f.get("description") or "")}
             for f in (who.get("login_flows") or []) if isinstance(f, dict)]
    logins = [_login(x) for x in (who.get("logins") or []) if isinstance(x, dict)]
    return {"id": b.id, "name": str(net.get("displayname") or b.id),
            "network": str(net.get("network_id") or net.get("beeper_bridge_type") or b.id),
            "icon": f"/bridges/{_q(b.id)}/icon" if mxc else None,
            "reachable": True, "error": None, "flows": flows, "logins": logins}


def listing() -> list[dict]:
    """`GET /bridges`: every bridge, asked at once, in `name` order."""
    found = discover()
    if not found:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(found))) as pool:
        got = list(pool.map(entry, found))
    return sorted(got, key=lambda e: (e["name"].casefold(), e["id"]))


def icon(bridge_id: str) -> tuple[bytes, str] | None:
    """`GET /bridges/<id>/icon`: the network icon's bytes and type, fetched
    from the homeserver once and kept; None when there is none to have."""
    b = find(bridge_id)
    if b is None:
        return None
    with _LOCK:
        mxc = _ICON_MXC.get(b.id)
    if mxc is None:
        entry(b)
        with _LOCK:
            mxc = _ICON_MXC.get(b.id)
    if not mxc or not mxc.startswith("mxc://") or "/" not in mxc[6:]:
        return None
    with _LOCK:
        if mxc in _ICONS:
            return _ICONS[mxc]
    server, media = mxc[6:].split("/", 1)
    hs = b.homeserver or (os.environ.get("MATRIX_HOMESERVER") or "").rstrip("/")
    if not hs:
        return None
    token = os.environ.get("MATRIX_OWNER_TOKEN") or os.environ.get("MATRIX_ACCESS_TOKEN") or ""
    tail = f"{_q(server)}/{_q(media)}"
    tries = [(f"{hs}/_matrix/client/v1/media/download/{tail}", token)] if token else []
    tries.append((f"{hs}/_matrix/media/v3/download/{tail}", ""))
    for url, tok in tries:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"} if tok else {})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read(2 * 1024 * 1024 + 1)
                ctype = resp.headers.get("Content-Type") or "application/octet-stream"
        except (urllib.error.URLError, OSError):
            continue
        if not data or len(data) > 2 * 1024 * 1024 or not ctype.startswith("image/"):
            continue
        with _LOCK:
            _ICONS[mxc] = (data, ctype)
        return data, ctype
    return None


# --- login processes --------------------------------------------------------------

class Proc:
    """One sign-in, as the server holds it."""

    def __init__(self, pid: str, bridge: Bridge):
        self.id = pid
        self.bridge = bridge
        self.status = "step"
        self.error: str | None = None
        self.step: dict | None = None
        self.touched = time.monotonic()
        self.busy = False          # a submit is with the bridge
        self.waiter: threading.Thread | None = None
        self.lock = threading.Lock()

    def view(self) -> dict:
        with self.lock:
            return {"id": self.id, "bridge": self.bridge.id, "status": self.status,
                    "error": self.error, "step": self.step}


_PROCS: dict[tuple[str, str], Proc] = {}
_PROCS_LOCK = threading.Lock()


def _sweep() -> None:
    cut = time.monotonic() - PROC_TTL_S
    with _PROCS_LOCK:
        for k in [k for k, p in _PROCS.items() if p.touched < cut]:
            del _PROCS[k]


def _proc(bridge_id: str, pid: str) -> Proc:
    _sweep()
    with _PROCS_LOCK:
        p = _PROCS.get((bridge_id, pid))
    if p is None:
        raise KeyError(pid)
    p.touched = time.monotonic()
    return p


def _absorb(p: Proc, resp: dict) -> None:
    """Take the bridge's answer (a LoginStep plus `login_id`) as the
    process's current step, starting the wait on a `display_and_wait`.
    Called with `p.lock` held."""
    step = {k: v for k, v in resp.items() if k != "login_id"}
    p.step = step
    p.error = None
    p.touched = time.monotonic()
    kind = step.get("type")
    if kind == "complete":
        p.status = "complete"
    elif kind == "display_and_wait":
        p.status = "waiting"
        # From inside the wait thread (a QR that changed) it is alive and
        # simply goes round again.
        if p.waiter is None or not p.waiter.is_alive():
            p.waiter = threading.Thread(target=_wait, args=(p,), daemon=True,
                                        name=f"bridge-wait-{p.bridge.id}")
            p.waiter.start()
    else:
        p.status = "step"


def _fail(p: Proc, message: str) -> None:
    """Called with `p.lock` held: the process is over."""
    p.status = "failed"
    p.error = message or "the bridge refused"
    p.touched = time.monotonic()


def _wait(p: Proc) -> None:
    """The background wait: submit each `display_and_wait` step (the bridge
    answers when the wait ends) until the process moves to another kind of
    step, ends, or is cancelled here."""
    while True:
        with p.lock:
            if p.status != "waiting" or not p.step:
                return
            step = dict(p.step)
        try:
            resp = _call(p.bridge, "POST",
                         f"/login/step/{_q(p.id)}/{_q(str(step.get('step_id') or ''))}/display_and_wait",
                         body={}, query={"txn_id": str(step.get("txn_id") or "")},
                         timeout=WAIT_TIMEOUT_S)
        except (BridgeError, Unreachable) as e:
            with p.lock:
                if p.status == "waiting" and p.step == step:
                    _fail(p, str(e))
            return
        with p.lock:
            if p.status != "waiting" or p.step != step:
                return   # cancelled (or moved on) while the bridge waited
            _absorb(p, resp)
            if p.status != "waiting":
                return


def start(bridge_id: str, flow: str) -> dict:
    """`POST /bridges/<id>/login {"flow"}`: the new process. LookupError for
    no such bridge, ValueError for no such flow; BridgeError/Unreachable
    when the bridge refuses or is down."""
    b = find(bridge_id)
    if b is None:
        raise LookupError("no such bridge")
    who = _call(b, "GET", "/whoami", timeout=WHOAMI_TIMEOUT_S)
    flows = {str(f.get("id")) for f in (who.get("login_flows") or []) if isinstance(f, dict)}
    if not flow or flow not in flows:
        raise ValueError("no such flow")
    resp = _call(b, "POST", f"/login/start/{_q(flow)}", body={}, timeout=STEP_TIMEOUT_S)
    pid = str(resp.get("login_id") or "") or f"local-{uuid.uuid4().hex}"
    p = Proc(pid, b)
    with p.lock:
        _absorb(p, resp)
    _sweep()
    with _PROCS_LOCK:
        _PROCS[(b.id, pid)] = p
    return p.view()


def get(bridge_id: str, pid: str) -> dict:
    """`GET /bridges/<id>/login/<process>`. KeyError when not held."""
    return _proc(bridge_id, pid).view()


def submit(bridge_id: str, pid: str, txn_id: str, values: dict) -> dict:
    """`POST …/step {"txn_id", "values"}`. KeyError when not held, Conflict
    when `txn_id` is not the current step's (or one is already with the
    bridge), ValueError when the current step is not one that is
    submitted."""
    p = _proc(bridge_id, pid)
    with p.lock:
        step = p.step or {}
        if p.busy or p.status not in ("step", "waiting") or not txn_id \
                or txn_id != step.get("txn_id"):
            raise Conflict("step moved on")
        kind = str(step.get("type") or "")
        if kind not in _SUBMITTED:
            raise ValueError(f"a {kind or 'blank'} step is not submitted")
        p.busy = True
    body = {str(k): "" if v is None else str(v) for k, v in (values or {}).items()}
    try:
        resp = _call(p.bridge, "POST",
                     f"/login/step/{_q(p.id)}/{_q(str(step.get('step_id') or ''))}/{kind}",
                     body=body, query={"txn_id": txn_id}, timeout=STEP_TIMEOUT_S)
    except (BridgeError, Unreachable) as e:
        with p.lock:
            p.busy = False
            if p.status != "cancelled":
                _fail(p, str(e))
        # The bridge ends a process on a failed step; make sure of it.
        try:
            _call(p.bridge, "POST", f"/login/cancel/{_q(p.id)}", body={})
        except (BridgeError, Unreachable):
            pass
        return p.view()
    with p.lock:
        p.busy = False
        if p.status != "cancelled":
            _absorb(p, resp)
    return p.view()


def cancel(bridge_id: str, pid: str) -> dict:
    """`POST …/cancel`: cancelled at the bridge (a bridge that already let it
    go, or is down, is cancelled here all the same)."""
    p = _proc(bridge_id, pid)
    with p.lock:
        over = p.status in ("complete", "failed", "cancelled")
        if not over:
            p.status = "cancelled"
            p.error = None
    if not over:
        try:
            _call(p.bridge, "POST", f"/login/cancel/{_q(p.id)}", body={})
        except (BridgeError, Unreachable):
            pass
    return p.view()


def logout(bridge_id: str, login_id: str) -> dict:
    """`POST /bridges/<id>/logout/<login id>`: that login signed out; the
    bridge's listing entry after. LookupError for no such bridge."""
    b = find(bridge_id)
    if b is None:
        raise LookupError("no such bridge")
    _call(b, "POST", f"/logout/{_q(login_id)}", body={})
    return entry(b)


def _reset_for_tests() -> None:
    with _PROCS_LOCK:
        _PROCS.clear()
    with _LOCK:
        _ICON_MXC.clear()
        _ICONS.clear()
