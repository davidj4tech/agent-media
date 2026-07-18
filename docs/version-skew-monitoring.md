# Cross-Host Version Skew Monitoring

When agent packages are distributed across a cluster (e.g., `mel` for the main brain, `p8ar` for phone-local tools, `red5` for rooms), edge nodes often get left behind when the main repo is updated.

To prevent silent failures from stale code, we use a **cron-free, decentralized version skew monitor** built into the package's existing tmux status line.

## The Pattern

Instead of managing systemd timers or crontabs on every machine, the monitoring piggybacks on something that already runs constantly: the tmux status bar. 

It has three components:

1. **The Ledger**: A simple text file at `~/.local/state/<package>/version-skew.log` containing the names of hosts that are out of date.
2. **The Status Hook**: The fast-running status command (`media status`, `session status`, etc.) reads the ledger. If the ledger is older than 2 hours, it spawns the doctor in the background. If the ledger contains hosts, it flashes an alert.
3. **The Doctor**: A background process that checks the local `git rev-parse HEAD` against the remote clones via SSH, updating the ledger.

### 1. The Status Hook & Auto-Trigger

Drop this into the command that your tmux status bar polls. It's fully asynchronous so it never blocks tmux redraws.

```python
import os, sys, time, subprocess
from pathlib import Path

def _skew_alert_line() -> str:
    """Read the skew ledger and auto-trigger background checks."""
    try:
        d = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
        logdir = d / "my-agent-package"
        logdir.mkdir(parents=True, exist_ok=True)
        ledger = logdir / "version-skew.log"
        
        # Async check every 2 hours (7200 seconds)
        try:
            mtime = ledger.stat().st_mtime
        except FileNotFoundError:
            mtime = 0
            
        if time.time() - mtime > 7200:
            ledger.touch()  # prevent concurrent spawns from rapid tmux redraws
            subprocess.Popen(
                [sys.argv[0], "doctor"],  # Or however your CLI invokes the doctor
                start_new_session=True, 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
            
        skew = ledger.read_text().strip()
        if not skew:
            return ""
            
        # Blink the warning icon
        glyph = "⚠" if int(time.time()) % 2 else " "
        return f"{glyph} skew: {skew.replace(chr(10), ', ')}"
    except OSError:
        return ""
```

### 2. The Doctor Command

The doctor compares the local git commit hash to the target hosts. Since `~/.ssh/config` handles the ControlMaster multiplexing, these checks are fast.

```python
def cmd_doctor(a) -> int:
    import subprocess, os
    from pathlib import Path
    
    hosts = os.environ.get("MY_PACKAGE_HOSTS", "p8ar red5 sp4").split()
    repos = ["my-agent-package", "dotfiles"]  # Check shared dependencies too
    skewed = []
    
    # 1. Gather local hashes
    local_hashes = {}
    for r in repos:
        path = str(Path.home() / "projects" / r) if r != "dotfiles" else str(Path.home() / r)
        try:
            local_hashes[r] = subprocess.run(
                ["git", "-C", path, "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True
            ).stdout.strip()
        except OSError:
            pass

    # 2. Check remote hosts
    for host in hosts:
        host_skewed = False
        for r, l_hash in local_hashes.items():
            r_path = f"~/{r}"  # Adjust to where clones live on the edge nodes
            try:
                res = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
                     f"git -C {r_path} rev-parse HEAD"],
                    capture_output=True, text=True, timeout=12)
                
                if res.returncode == 0 and res.stdout.strip() != l_hash:
                    host_skewed = True
            except Exception:
                pass
                
        if host_skewed:
            skewed.append(host)

    # 3. Write Ledger
    d = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    ledger = d / "my-agent-package" / "version-skew.log"
    try:
        if skewed:
            ledger.write_text("\n".join(skewed) + "\n")
            return 1
        else:
            ledger.unlink(missing_ok=True)
            return 0
    except OSError:
        return 1
```

## Applying to Other Packages

To drop this into `agent-sessions`, `agent-workspace`, or `pi-workspace`:
1. Pick a CLI command that gets polled frequently by tmux (or add a dedicated `pkg-status` script).
2. Wire in the `_skew_alert_line` function so it overrides the normal status output when an alert is active.
3. Adjust the `repos` list in the `doctor` function to include the specific repos that package cares about.