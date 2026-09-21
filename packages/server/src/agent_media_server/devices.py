"""Paired devices: the app's own credential (server-contract.md §9).

The Audiobookshelf login stood in for "who is this?" while the app lived
inside ABS. It leaves with ABS, so the app needs a credential of its own, and
the one decided on (21 Sep 2026) is a token per device, minted by pairing and
revocable one device at a time.

How a device gets one:

1. At the desk, `media-visual-canvas pair --device "Pixel 8a"` mints a
   pairing code — 8 hex chars, valid for `pair_ttl()` (30 min by default) —
   and prints a link and a QR the app scans.
2. The app sends the code to `POST /pair`. A good code is burned and a token
   comes back: `secrets.token_urlsafe(32)`, 43 characters. That response is
   the only time the token exists anywhere but on the phone.
3. The app sends `Authorization: Bearer <token>` wherever it sent the ABS
   bearer. `lookup` recognises it with no network call, which is the point:
   an ABS outage can no longer log anybody out, and auth can no longer 503.

What is kept, in `<state_dir>/devices.json` (mode 600):
`[{"id": "d_<hex>", "name", "sha256", "created", "last_seen", "last_ip"}]`.
Only the sha256 of each token is stored, so the file is worth nothing to
someone who reads it — they learn which devices exist, not how to be one. A
token is a 256-bit random string, not a password, so a plain hash is the
right tool: there is nothing to brute-force that a salt or a slow hash would
protect.

The pairing codes are a SEPARATE store from the canvas's own `GET /pair`
code (the one that installs the amux token into a browser, in the spool's
`pair-code`). The two must never unlock each other: the amux token types into
panes with no questions asked, and a code minted to pair a phone must not be
a way to be handed it — nor the other way round. Different files, different
routes, different code paths; nothing here reads the spool, and the canvas's
`_pair_consume` never reads this.

Codes, and why a failed attempt does not burn one: a code that died on its
first wrong guess would let anyone on the tailnet cancel a pairing by
guessing badly. Instead a code lives out its window, dies on its first
*successful* use, and failures are rate-limited per source address
(`MAX_FAILURES` in `FAIL_WINDOW_S`) — 10 guesses at 2**32 is not a search.

Config (env):
  MEDIA_DEVICE_PAIR_TTL   seconds a device pairing code stays valid (default
                          MEDIA_VISUAL_PAIR_TTL, else 1800 — the same window
                          as the canvas's own pairing link)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path

#: One lock for both files. Every handler thread that authenticates may
#: write `last_seen`, and a pairing writes both files together; one lock is
#: simpler than two and contention is nil (a write happens at most once a
#: minute per device).
_LOCK = threading.RLock()

#: `last_seen` / `last_ip` are written at most this often per device. Without
#: a floor every poll (the app polls `/sessions/state` every 5 s) would
#: rewrite the file.
SEEN_EVERY_S = 60.0

#: Pairing failures allowed per source address before `POST /pair` answers
#: 429, and the window they are counted over.
MAX_FAILURES = 10
FAIL_WINDOW_S = 600.0
_FAILS: dict[str, deque] = {}

# Parsed devices.json, keyed by the file's (mtime_ns, size), so a request does
# not parse JSON just to find nobody changed it — and a `devices --revoke`
# from the CLI (another process) is seen on the very next request, because
# the revoke changes the mtime.
_CACHE: tuple[tuple[int, int] | None, list] = (None, [])


def pair_ttl() -> int:
    """How long a device pairing code is good for, in seconds. Read per call so
    a test (or a host) can change it without a restart."""
    for var in ("MEDIA_DEVICE_PAIR_TTL", "MEDIA_VISUAL_PAIR_TTL"):
        raw = (os.environ.get(var) or "").strip()
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
    return 1800


def _dir() -> Path:
    from agent_media_core._paths import state_dir

    return state_dir()


def devices_path() -> Path:
    return _dir() / "devices.json"


def codes_path() -> Path:
    return _dir() / "device-pair-codes.json"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _write(path: Path, data) -> None:
    """Replace `path` with `data` as JSON, atomically and owner-only.

    The temp file is created 600 before a byte is written — chmod after the
    fact would leave a window where it is world-readable — and renamed over
    the real one, so a reader never sees half a file and a crash never leaves
    an empty one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _read(path: Path, default):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


# --- the devices ------------------------------------------------------------------

def _load() -> list:
    """devices.json, through the mtime cache. Call with `_LOCK` held."""
    global _CACHE
    p = devices_path()
    try:
        st = p.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        _CACHE = (None, [])
        return []
    if _CACHE[0] != stamp:
        rows = [d for d in _read(p, []) if isinstance(d, dict) and d.get("sha256")]
        _CACHE = (stamp, rows)
    return _CACHE[1]


def _save(rows: list) -> None:
    global _CACHE
    _write(devices_path(), rows)
    _CACHE = (None, [])


def lookup(token: str, ip: str = "") -> dict | None:
    """The device this bearer belongs to, or None. Local only — never a network
    call, whatever the answer.

    Every stored hash is compared, with `hmac.compare_digest`, and the loop
    does not stop at the first match: how long the answer takes should not say
    how far down the list the device was. (The hash is what is compared, so
    even a leaky compare would leak bits of a hash, not of a token — this is
    belt and braces.)

    A hit also stamps `last_seen` and `last_ip`, no more than once a minute.
    """
    token = (token or "").strip()
    if not token:
        return None
    want = _hash(token)
    with _LOCK:
        rows = _load()
        hit = None
        for d in rows:
            if hmac.compare_digest(str(d.get("sha256") or ""), want):
                hit = d
        if hit is None:
            return None
        now = time.time()
        if now - float(hit.get("last_seen") or 0) >= SEEN_EVERY_S:
            fresh = [dict(d) for d in rows]
            for d in fresh:
                if d.get("id") == hit.get("id"):
                    d["last_seen"] = round(now, 3)
                    d["last_ip"] = ip or d.get("last_ip") or ""
                    hit = d
            try:
                _save(fresh)
            except OSError:
                # A read-only or full disk must not turn a good token away:
                # `last_seen` is a courtesy, the token is the credential.
                pass
        return {k: v for k, v in hit.items() if k != "sha256"}


def list_devices() -> list[dict]:
    """Every paired device, without its hash."""
    with _LOCK:
        return [{k: v for k, v in d.items() if k != "sha256"} for d in _load()]


def revoke(device_id: str) -> bool:
    """Forget a device. Its token answers 401 on its next request (it is then
    simply unknown, and falls through to the ABS check, which refuses it)."""
    with _LOCK:
        rows = _load()
        keep = [d for d in rows if d.get("id") != device_id]
        if len(keep) == len(rows):
            return False
        _save(keep)
        return True


# --- pairing ----------------------------------------------------------------------

def mint_code(name: str) -> tuple[str, float]:
    """A fresh pairing code for a device called `name`: `(code, expires)`.

    Several may be outstanding at once (pairing a phone and a tablet in the
    same sitting); expired ones are swept whenever the file is written.
    """
    code = secrets.token_hex(4)
    expires = time.time() + pair_ttl()
    with _LOCK:
        now = time.time()
        codes = {c: v for c, v in _read(codes_path(), {}).items()
                 if isinstance(v, dict) and float(v.get("expires") or 0) > now}
        codes[code] = {"name": " ".join((name or "").split())[:80],
                       "expires": round(expires, 3)}
        _write(codes_path(), codes)
    return code, expires


def rate_limited(ip: str) -> bool:
    """Whether `ip` has used up its pairing failures for now."""
    now = time.time()
    with _LOCK:
        q = _FAILS.get(ip)
        if not q:
            return False
        while q and now - q[0] > FAIL_WINDOW_S:
            q.popleft()
        return len(q) >= MAX_FAILURES


def _note_failure(ip: str) -> None:
    with _LOCK:
        _FAILS.setdefault(ip, deque()).append(time.time())


def redeem(code: str, device: str, ip: str = "") -> dict | None:
    """Trade a pairing code for a device token. `{"token", "device_id",
    "name"}`, or None for a code that is wrong, used or out of date (and the
    failure counts against `ip`).

    The name is the one given at the desk (`pair --device NAME`) when there
    was one: that is the person with the shell deciding what this device is
    called, and a device should not get to name itself something else. The
    name the device sends is used only when the desk gave none.
    """
    code = (code or "").strip().lower()
    now = time.time()
    with _LOCK:
        codes = {c: v for c, v in _read(codes_path(), {}).items()
                 if isinstance(v, dict)}
        hit = None
        for c in codes:
            if hmac.compare_digest(c, code):
                hit = c
        entry = codes.get(hit) if hit else None
        if not code or entry is None or float(entry.get("expires") or 0) <= now:
            _note_failure(ip)
            return None
        # Burn the code BEFORE the device is written: if the second write
        # failed, a code that still worked would be a second device for the
        # price of one.
        del codes[hit]
        codes = {c: v for c, v in codes.items() if float(v.get("expires") or 0) > now}
        _write(codes_path(), codes)
        token = secrets.token_urlsafe(32)
        name = entry.get("name") or " ".join((device or "").split())[:80] or "device"
        row = {"id": "d_" + secrets.token_hex(6), "name": name, "sha256": _hash(token),
               "created": round(now, 3), "last_seen": round(now, 3), "last_ip": ip or ""}
        _save(list(_load()) + [row])
    return {"token": token, "device_id": row["id"], "name": name}


def _reset_for_tests() -> None:
    global _CACHE
    with _LOCK:
        _FAILS.clear()
        _CACHE = (None, [])


# --- the CLI ----------------------------------------------------------------------

def links(code: str, host: str, port: int) -> tuple[str, str]:
    """`(app link, browser link)` for a device pairing code.

    The app reads `server` and `code` out of the first. The second is for a
    person reading the terminal — it names the same code against the same
    host — and is NOT the canvas's amux page: `&device=1` is only a label for
    the reader, and `GET /pair` will refuse this code, because it looks in a
    different store.
    """
    from urllib.parse import quote

    base = f"http://{host}:{port}"
    return (f"sasonica://pair?server={quote(base, safe='')}&code={code}",
            f"{base}/pair?c={code}&device=1")


def cli_devices(argv: list[str]) -> int:
    """`media-visual-canvas devices [--revoke ID]` — list, or forget one."""
    import argparse

    ap = argparse.ArgumentParser(prog="media-visual-canvas devices",
                                 description="List paired devices, or revoke one.")
    ap.add_argument("--revoke", metavar="ID", help="forget this device (its token stops working)")
    args = ap.parse_args(argv)
    if args.revoke:
        if revoke(args.revoke):
            print(f"revoked {args.revoke}")
            return 0
        print(f"no device {args.revoke}", flush=True)
        return 1
    rows = list_devices()
    if not rows:
        print("no paired devices")
        return 0

    def when(t) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(t))) if t else "-"

    for d in rows:
        print(f"{d.get('id'):<16} {d.get('name', ''):<24} paired {when(d.get('created'))}"
              f"  seen {when(d.get('last_seen'))} from {d.get('last_ip') or '-'}")
    return 0
