"""Record off-machine access with the thread and the code that made it.

Opt-in, and reporting only:

    python -m pytest -p tests.phone_guard      # from packages/core

A unit suite that quietly talks to the fleet passes for reasons nobody wrote
down. `test_prev_restart` did exactly that: `_restart_current_playlist` ends at
the speech player, the test reached the real phone, and it passed only because
the app refused `seek` and the call failed into the answer the test wanted. The
day the app learned to seek, two tests began describing a device instead of the
code.

Attribution by "whichever test was running" is a lie when the connect happens
on a background thread a previous test left behind, so the thread and the
calling chain are recorded too, and a background hit says so.

NOT wired into the default run, and it should not be. Wrapping `connect` adds
enough latency to trip `test_display_breaker`, which deliberately treats
anything slow as slow — an instrument that changes what it measures is fine to
reach for on purpose and wrong to leave switched on.
"""

import socket
import subprocess
import threading
import traceback

HITS = {}          # (nodeid, thread, frame) -> count
_current = {"id": "<collection>"}

_real_connect = socket.socket.connect
_real_popen = subprocess.Popen.__init__
_real_run = subprocess.run


def _where():
    ours = [f for f in traceback.extract_stack()[:-2]
            if "/agent_media_core/" in f.filename]
    if not ours:
        return "<outside the package>"
    trail = [f"{f.filename.split('/agent_media_core/')[-1]}:{f.lineno} {f.name}"
             for f in ours[-4:]]
    return " <- ".join(reversed(trail))


def _note(what):
    key = (_current["id"], threading.current_thread().name, _where(), what)
    HITS[key] = HITS.get(key, 0) + 1


def _remote(addr):
    host = addr[0] if isinstance(addr, tuple) else None
    if not isinstance(host, str):
        return None
    if host.startswith("127.") or host in ("localhost", "::1", "0.0.0.0", ""):
        return None
    return host


def _connect(self, addr):
    host = _remote(addr)
    if host:
        _note(f"tcp {host}:{addr[1]}")
    return _real_connect(self, addr)


def _watch(args):
    exe = args[0] if isinstance(args, (list, tuple)) and args else ""
    base = str(exe).rsplit("/", 1)[-1]
    if base in ("ssh", "scp", "adb", "rsync"):
        _note(f"exec {base}")


def _popen_init(self, args=None, *a, **kw):
    _watch(args)
    return _real_popen(self, args, *a, **kw)


def _run(*a, **kw):
    if a:
        _watch(a[0])
    return _real_run(*a, **kw)


socket.socket.connect = _connect
subprocess.Popen.__init__ = _popen_init
subprocess.run = _run


def pytest_runtest_setup(item):
    _current["id"] = item.nodeid


def pytest_sessionfinish(session, exitstatus):
    if not HITS:
        print("\n[phone-guard] nothing reached off this machine")
        return
    print("\n[phone-guard] off-machine access:")
    for (nodeid, thread, where, what), n in sorted(HITS.items()):
        main = "MainThread" == thread
        tag = "" if main else f"  [background thread {thread} — the test named"
        tag += "" if main else " here was merely running at the time]"
        print(f"  {what} x{n}\n      during: {nodeid}\n"
              f"      thread: {thread}\n      from:   {where}{tag}")
