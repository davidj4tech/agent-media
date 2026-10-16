"""deploy/install.ps1's Sasonica Shell step, through check-install-ps1.ps1,
wherever PowerShell is (the Windows CI job: Windows PowerShell 5.1, the one
`irm | iex` runs in). Stand-ins only: no download, no relay, no server."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def _powershell() -> str | None:
    return shutil.which("powershell") if os.name == "nt" else None


@pytest.mark.skipif(_powershell() is None, reason="Windows PowerShell: Windows only")
def test_install_ps1_shell_step():
    env = {k: v for k, v in os.environ.items() if not k.startswith("SASONICA_")}
    r = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", str(HERE / "check-install-ps1.ps1"), str(REPO)],
                       env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
    print(r.stdout, r.stderr)
    assert r.returncode == 0 and "all passed" in r.stdout, r.stdout + r.stderr
