"""Other processes, read the same way on Linux and a Mac.

docs/proposals/2026-09-23-cross-platform.md, step 1: the posix assumptions in
one place. Linux reads `/proc/<pid>/…`; macOS has no /proc, so the same
answers come from `ps` (a whole table in one call) and `lsof` (a working
directory). Every function answers "don't know" — [], {} or "" — rather than
raising, as a read of a process that has just ended does on Linux.

On a Mac `ps -E` prints a process's environment after its arguments, with
nothing between them; `environ` keeps the trailing `NAME=value` words, which
is exact for the markers read here (TMUX_PANE, MEDIA_SESSIOND_SESSION…) and
only ever misses a value with a space in it. Only the user's own processes
show their environment, which is all we look at.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

PROC = Path("/proc")
_ENV_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def has_proc() -> bool:
    return (PROC / "self").exists()


def _ps(*args: str) -> str:
    try:
        return subprocess.run(["ps", *args], capture_output=True, text=True,
                              timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def processes() -> list[tuple[int, list[str]]]:
    """`(pid, argv)` for every process that can be read."""
    out = []
    if has_proc():
        for d in PROC.iterdir():
            if not d.name.isdigit():
                continue
            try:
                argv = [a.decode(errors="replace")
                        for a in (d / "cmdline").read_bytes().split(b"\0") if a]
            except OSError:
                continue
            out.append((int(d.name), argv))
        return out
    for line in _ps("-axww", "-o", "pid=,args=").splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit():
            out.append((int(pid), args.split()))
    return out


def environ(pid: int) -> dict[str, str]:
    if has_proc():
        try:
            raw = (PROC / str(pid) / "environ").read_bytes().split(b"\0")
        except OSError:
            return {}
        return {k.decode(errors="replace"): v.decode(errors="replace")
                for k, _, v in (e.partition(b"=") for e in raw if b"=" in e)}
    words = _ps("-E", "-ww", "-o", "args=", "-p", str(pid)).split()
    env: dict[str, str] = {}
    for w in reversed(words):
        if not _ENV_WORD.match(w):
            break
        k, _, v = w.partition("=")
        env.setdefault(k, v)
    return env


def environs() -> dict[int, dict[str, str]]:
    """Every readable process's environment at once (one `ps` on a Mac)."""
    if has_proc():
        return {pid: environ(pid) for pid, _ in processes()}
    out: dict[int, dict[str, str]] = {}
    for line in _ps("-E", "-axww", "-o", "pid=,args=").splitlines():
        pid, _, rest = line.strip().partition(" ")
        if not pid.isdigit():
            continue
        env: dict[str, str] = {}
        for w in reversed(rest.split()):
            if not _ENV_WORD.match(w):
                break
            k, _, v = w.partition("=")
            env.setdefault(k, v)
        out[int(pid)] = env
    return out


def alive(pid: int) -> bool:
    """Whether process `pid` exists (someone else's counts).

    Not `os.kill(pid, 0)` on Windows: there signal 0 is CTRL_C_EVENT, and
    os.kill sends Ctrl+C to every process on the console — it ended CI's
    test run with a KeyboardInterrupt, and would end the server's own
    processes. There the process is opened and its exit code read instead.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.GetLastError() == 5      # ERROR_ACCESS_DENIED: there, not ours
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259
        finally:
            k32.CloseHandle(h)                     # 259: STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True        # EPERM: alive, someone else's
    return True


def image(pid: int) -> str:
    """The process's executable name, lower-case ("claude.exe"); "" when it is
    not running or cannot be read. Windows only needs it (`tasklist`); other
    systems answer from `processes`."""
    if os.name != "nt":
        for p, argv in processes():
            if p == pid and argv:
                return os.path.basename(argv[0]).lower()
        return ""
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    line = out.strip().splitlines()[0] if out.strip() else ""
    return line.split('","')[0].strip('"').lower() if line.startswith('"') else ""


def cwd(pid: int) -> str:
    """The process's working directory, resolved; "" when it cannot be read."""
    if has_proc():
        try:
            return os.path.realpath(os.readlink(PROC / str(pid) / "cwd"))
        except OSError:
            return ""
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        if line.startswith("n"):
            return os.path.realpath(line[1:])
    return ""
