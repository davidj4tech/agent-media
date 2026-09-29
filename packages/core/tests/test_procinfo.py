"""procinfo: other processes read one way on every platform."""

import re
import subprocess
import sys
from pathlib import Path

from agent_media_core import procinfo


def test_alive():
    import os

    assert procinfo.alive(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not procinfo.alive(p.pid)
    assert not procinfo.alive(0) and not procinfo.alive(None) and not procinfo.alive("x")


def test_nothing_else_probes_a_pid_with_signal_0():
    """`os.kill(pid, 0)` is Ctrl+C to the whole console on Windows (CI's run
    ended in a KeyboardInterrupt): liveness goes through procinfo.alive."""
    root = Path(procinfo.__file__).resolve().parents[2]      # packages/core
    found = []
    for pkg in ("core", "server", "visual", "sasonica"):
        for f in (root.parent / pkg / "src").rglob("*.py"):
            if f.name == "procinfo.py":
                continue
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\bkill\([^,()]+(\([^)]*\))?,\s*0\)", line):
                    found.append(f"{f.name}:{n}")
    assert not found, found
