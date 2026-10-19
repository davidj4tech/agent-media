# Cross-Host Version Skew Monitoring

When agent packages are distributed across a cluster (e.g., `mel` for the main brain, `p8a` for phone-local tools, `red5` for rooms), edge nodes often get left behind when the main repo is updated.

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
    
    hosts = os.environ.get("MY_PACKAGE_HOSTS", "p8a red5 sp4").split()
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

## Beyond skew: can the code actually run?

Skew monitoring answers "is this host on the right commit?" — which turns out
not to be the same question as "does this host work". On 2026-07-30 the phone
was on the correct commit while every entrypoint raised
`ModuleNotFoundError: agent_media_core`: Termux had upgraded python 3.13 → 3.14
in place, stranding the site-packages the install lived in. `media-mcp`
crash-looped roughly 1250 times overnight and nothing said a word, because the
only health signal in the system was a git hash comparison that read perfectly
healthy.

Three additions, each covering a different failure mode:

### `media selfcheck`

Prints this host's install health as `key=value` lines: interpreter version,
module path, `install=editable|copy`, checkout commit, service states,
crash-loop counts. `media doctor` now runs it on every host in the same ssh
round trip that fetches the git revisions, and reports both kinds of trouble.

`install=copy` deserves its own mention. The phone's venv held a *non-editable*
copy of the package, and `call-guard` runs out of that venv — so `git pull`
deployed nothing to it and it ran whatever the code was on install day. Nothing
was broken enough to notice; it was simply the wrong code, indefinitely.

The same trap one level down: **service files that are copies.** p8a's
`phone-jobs` ran copies of its `run` and `phone-jobs.py` made on 3 Oct; every
pull since updated the checkout and nothing the service ran (found 18 Oct
2026). selfcheck now compares (`agent_media_core/service_drift.py`):

- **runit**: each service dir named like a template in the checkout
  (`packages/*/services/<name>`, `deploy/*/service/<name>`), file by file
  (`run`, `log/run`, `finish`, a worker script). A dir or file that links
  into the checkout is fine; `supervise/` is runit's and skipped.
- **systemd --user**: each unit in `~/.config/systemd/user` named like one
  in the checkout (`packages/*/systemd`, `packages/*/deploy`,
  `deploy/systemd`) that is a regular file. A unit `media-setup
  install-services` generated (`ExecStart=/bin/sh <checkout>/…/run`) is not
  a copy: the code it runs is the checkout's.
- **Windows**: its Task Scheduler tasks name a program and hold no copy of a
  file, so there is nothing to compare.

`drift=` lists the copies that **differ**: a problem, so the host goes `!`
in the ledger (a pull cannot fix it, which is what `!` means). `copies=`
lists the ones that still match: doctor prints them as "copy, not linked",
not a problem until the next pull changes the original. A deliberate local
copy is kept out with `MEDIA_DRIFT_OK="<unit> <service>/<file>"` in the
environment selfcheck runs in.

The fix is a link:

```
media-setup link-services --dry-run
media-setup link-services            # identical copies → links (copy kept as <file>.bak-<date>)
media-setup link-services --force    # also the ones that differ (compare first)
```

`media-setup install-services` installs runit services as links only: a real
service dir that matches its template becomes a link, one that differs is
reported and left alone unless `--force` (then moved to
`~/.local/share/agent-media/service-backups/<name>.bak-<date>` first). The
phone's own services (`deploy/phone/service`: `phone-jobs`, `say-http`,
`music-files`) install the same way when named:
`media-setup install-services phone-jobs`.

Hosts too old to have the subcommand answer `selfcheck=unsupported` and are
reported as skewed (which they are), never as broken. A host whose install is
genuinely dead cannot run `media selfcheck` to say so, so the remote probe falls
back to importing via the venv python directly — the distinction between
"broken" and "just old" has to survive the thing being broken.

Unhealthy hosts land in the same ledger as skewed ones, suffixed with `!`, so
the status bar's existing ⚠ covers both (the label is now `fleet:`, not
`skew:`).

### `services/_common/crash-notify`

A runit `finish` script for each service. runit restarts a dead service
instantly, so a service that dies on startup spins as fast as the OS allows.
After 3 failures in 60s this backs off to one attempt per 10s and fires one
notification per 30 minutes, with the last log line as the body.

It is plain POSIX sh with no agent-media imports, on purpose: the failure it
exists to report is usually "the Python package won't import", and a notifier
written in the broken runtime cannot report its own death. It also records exit
codes to `~/.local/state/agent-media/sv-crash/<svc>.log`, which is what
`selfcheck` reads — and how `call-hold-consumer` was finally diagnosed (exit
111, once per second, forever: its shebang was `#!/usr/bin/env sh` and Android
has no `/usr/bin`).

### `scripts/termux-python-heal.sh`

Repairs the layout the other two check for: rebuilds `<checkout>/.venv` as an
editable install and relinks `$PREFIX/bin/media*` into it. `--install-hook`
registers it as an apt `DPkg::Post-Invoke`, so a Termux python upgrade repairs
itself instead of silently breaking the phone. `--check` reports without
touching anything; it is safe to run on non-Termux hosts (it understands both
symlinked and shebang-style console scripts).

agent-media was not the only casualty: `mopidy` and `beets` were `pip --user`
installs in the same stranded tree, and their runit services had been down just
as quietly. `scripts/termux-apps.conf` lists them, and the heal script gives
each its own venv — so "repair" means "rebuild", not "remember the original pip
incantation". Two guards, since this is code touching things it doesn't own:
the section is Termux-only (red5 runs the real house Mopidy and must not grow a
second one), and it only ever *adopts* an app the host already has. Only the
entrypoints named in the config are linked, because an app's venv also contains
scripts called `flask`, `pygmentize` and `normalizer`.

Versions there are pinned to what the phone was running, because this is a
restore rather than an upgrade — and because the current releases don't build
on Termux at all (beets 2.x pulls numpy, Mopidy 4.x pulls pygobject from PyPI;
both fail CMake). Mopidy 3 instead expects gi/GStreamer from the system, hence
`--system-site-packages`.

Which exposes the last layer: Termux ships some modules as **apt** packages
(`pygobject`, `gst-python`, `python-brotli`) and upgrading `python` does not
rebuild them — their files stay in the old `site-packages` and vanish from the
new interpreter's view. No venv can substitute for those. `--apt-repair`
reinstalls them; it detaches when apt holds its own lock, so it can't deadlock
against the post-invoke hook that may have called it.

## Acting on the warning: `media doctor --fix`

Detection was never the gap. The status bar re-checks on a timer (10 min while
a warning is up, 2 h when clean), so `⚠ fleet:` is always current — what was
missing was a way to act on it without leaving the pane and remembering the
command, and warnings you can only read are warnings you learn to live with.

`--fix` deploys the hosts that are **merely behind**: `git pull --ff-only`,
then `media restart-services`, then a re-probe of just those hosts so a good
deploy clears the ⚠ in the same run.

The restart is not optional and its position is not arbitrary. An editable
install means a pull updates the files, but every long-running service is still
executing the code it imported at start — and the *next* doctor run sees a
matching HEAD and calls that host healthy. Pulled-but-not-restarted is exactly
the silent-wrong-code state this whole system exists to catch. Restarting
*after* the pull also means a host too old to have the subcommand gains it in
the same round trip.

What it will not do:

- **Touch a host marked `!`.** That host is broken, not stale — dead install,
  services down, crash loop. A pull fixes none of those, and restarting a
  crash-looping service just spins it faster while the output claims work was
  done. Those want a person.
- **Force anything.** A dirty checkout is reported and skipped; `--ff-only`,
  never a reset or a stash. An unattended rewrite of a host David may be
  mid-edit on is worse than a warning that stays up.
- **Run itself.** The background re-check only ever diagnoses. A pull onto the
  phone restarts the speech renderer mid-sentence: fine when you chose the
  moment, startling when a timer did.

The trigger is `f` in the control popup (`prefix a`), which shows the report and
asks before deploying. It sits with the other things you *open* rather than do,
and inside the popup because the root key table is nearly full — `prefix H` is
oh-my-tmux's `resize-pane -L`, and media.tmux is sourced late enough to have
taken it silently.

`media restart-services` is also useful alone (`--dry-run` names what it would
touch). It restarts only what selfcheck is willing to grade: services
symlinked into the checkout plus the adopted Termux apps, never sshd or
snapclient, and never a runit service parked with a `down` file.

### Restarting safely, and from inside a chat

`media restart-services` is safe for an agent to run unattended (David,
18 Oct 2026). Its logic is `agent_media_core.restart_services.run()`, a plain
function that a server route on any host could call as well.

```
media restart-services [--only NAME …] [--after SECONDS] [--force] [--dry-run]
                       [--no-drain] [--cancel]
```

- `--only` restarts just those services. Short names work (`sessiond`,
  `canvas`, `tts-shim`, `sink-speech`, `speech-state`, `clips`, …), matched
  against the units listed above. An unknown name exits 2 and lists the
  known ones.
- **Busy checks** (exit 3, skipped by `--force`; `--dry-run` shows them):
  - **sessiond** is refused while any live session other than the caller's
    is working or starting, waiting on an approval, holding queued messages,
    or has `tasks` in flight. The caller is the session named by
    `MEDIA_SESSIOND_SESSION`, which sessiond puts in its children's
    environment. That session is also refused when it has more than one
    task: the one tolerated is the command itself, or the subagent that ran
    it. A restart of sessiond *now* from inside one of its sessions is
    refused as well, because it would end that chat.
  - **speech services** (tts-shim, sink-speech, speech-state, clips, the
    speech fifo) are refused while a reply is being spoken.
- `--after SECONDS` runs the restart later and returns at once with the unit
  name. Under systemd it is a transient user unit (`systemd-run --user
  --on-active=N --collect`), outside every caller's cgroup, so it survives
  the caller being killed; read it with `journalctl --user -u
  media-restart-…`. Elsewhere (runit, Windows) it is a detached child in its
  own session, logging to `$XDG_STATE_HOME/agent-media/restart-services.log`.
  When it fires it checks again, and this time the caller's session counts
  too, so a turn still going is waited for. While busy it retries every 15 s
  (`MEDIA_RESTART_RETRY_S`) for up to 10 min (`MEDIA_RESTART_WAIT_MAX_S`),
  then gives up and raises the alert below. With sessiond selected it
  drains instead (next item).
- **Draining** (19 Oct 2026; the default for `--after` with sessiond
  selected, `--no-drain` refuses a busy host as before). Waiting alone
  mostly failed: new messages kept sessions busy past the 10 min. Now a
  busy (or not) host gets a pending restart,
  `$XDG_STATE_HOME/agent-media/restart-pending.json`, and exit 0:
  - sessiond **holds** every new message to its sessions (kept in the
    session's record, so it outlives the restart; the app is answered
    `held: true`) and refuses new chats, so nothing becomes busy again.
    Answering a question card is not held.
  - each busy session is **told once** to finish the step it is on and
    end its turn, without starting background agents or commands
    (intake/context.py `_restart_note`). A running turn sees no
    UserPromptSubmit, so the note rides on a PostToolUse hook,
    `media-hook-context --restart`, added by `media setup`'s context row
    behind a shell test that runs nothing unless a restart is pending in a
    sessiond session. Subagents' tool calls are not told.
  - the detached run (`--fire --drain`) checks every 3 s
    (`MEDIA_RESTART_DRAIN_POLL_S`) and restarts **the moment nothing is
    busy**. A session busy only with a question or permission card does
    not count: the agent cannot finish it; the card is lost with the
    restart and the session is resumed to ask again.
  - after 30 min (`MEDIA_RESTART_DRAIN_MAX_S`) it gives up: the record
    goes, the held messages are delivered, the alert is raised. A run that
    stops checking in for 90 s holds nothing.
  - a second request while one waits **joins** it (its services added);
    `--cancel` drops it, releasing what it held. `--dry-run` says whether
    it would drain.
  The phone sees it as `restart_pending` on `/targets` and the thread
  (server-contract §6.1).
- **Resuming** (19 Oct 2026, any restart of sessiond, `--force` and a crash
  included). Before a stop parks anything, sessiond writes on each live
  session's record what it was doing (`cut_off`); a crashed instance's
  records still say it. At start it resumes each session that was cut off
  mid-turn, with a card up, or with background work, once, with a message
  from `[From the session host]` naming what was running and saying to pick
  up where it left off and restart any background agent or command it
  still needs (subagent transcripts' folder named). Idle sessions are not
  woken. Held messages are delivered after.
- **Health check:** every restarted service must be up within 20 s
  (`MEDIA_RESTART_HEALTH_S`), and a canvas must also answer HTTP. A timer's
  job is not checked. A failure raises the host alert
  `restart-services.<host>` (§6.17, via `agent-alert`, else the store
  in-process), which the phone sees. A clean restart reports it ok, which
  clears it.

From a phone-started chat, the form to use (and the one a permission rule can
allow) is:

```
media restart-services --only sessiond --after 10
```

The turn ends, and ten seconds later, or once the other busy sessions have
ended their turns (draining), sessiond restarts. A chat that was cut off is
resumed by itself; an idle one resumes on its next message.

### Restarting another host

`--host NAME` does the same on another host without ssh and without
Tailscale (David, 18 Oct 2026: "I don't want to have to rely on
tailscale"). It runs `restart_services.run()` there with the same options,
busy checks and health check, prints that run's lines and exits with its
code. `--dry-run` shows that host's busy checks. There are two ways there,
tried in this order.

**The phone: down its own connection.** The phone's canvas listens on
loopback only, so nothing can POST to it. Its Termux jobs worker
(`deploy/phone/service/phone-jobs`) already holds a connection open to
red5's server through the tunnel. On red5, when NAME names that worker's
device, the restart is queued on red5's own server (`POST /jobs/restart`,
contract §6.21a) and goes down that connection as a `restart` job. The
worker runs `run()` on the phone and answers with its result. To restart
the phone's canvas, on red5:

```
media restart-services --host p8a --only canvas
media restart-services --host p8a --only canvas --dry-run
```

NAME is the worker device's id, its whole name (`p8a Termux jobs`) or the
first word of its name, in any case: `p8a`. The device must be red5's
`MEDIA_PHONE_JOBS_DEVICE`. `canvas` means the canvas wherever it runs: the
phone's `canvas-local` here, red5's `agent-media-visual-canvas`. The caller
waits up to 60 s for the phone to say it did, scheduled or refused the
restart. A restart now waits out its 20 s health check first. If the
worker itself (`--only phone-jobs`) is among the services, it restarts
detached, 10 s later. Exit 1 when the worker is not connected (offline),
too old to take a restart (pull on the phone, then `sv restart
$PREFIX/var/service/phone-jobs`), or does not answer in time. Nothing is
left queued in any of these cases. The worker takes only the four fields
`only`, `after`, `force` and `dry_run`. It runs no command.

**Updating the phone the same way.** `media update --host p8a` (red5) pulls
the phone's checkout and restarts what it owns, down the same connection
(contract §6.21a, `POST /jobs/update`): `git pull --ff-only` bounded at 90 s,
refused when the checkout has uncommitted changes, then, only when HEAD
moved, `restart_services.run()` there with the busy checks, detached, 10 s
out (`--after` to change it, `--only` to narrow it). `--dry-run` fetches and
says what would come and what would restart. It does not `pip install`: a
change of dependencies still wants one by hand. `media doctor --fix` uses
this for a host that is one of red5's jobs devices, and for one ssh could
not reach (the phone off the tailnet); any other host is fixed over ssh as
before.

```
media update --host p8a --dry-run
media update --host p8a
```

**Any host the same way: its jobs worker.** The laptop (pn), the speaker
(sp4) or a Windows box gets the phone's arrangement: a worker that holds its
own connection open to red5 (`packages/core/services/jobs-worker`, the same
script as the phone's `phone-jobs.py`, run with `--generic`), paired as a
**jobs-only device**. It takes restarts and updates, never a command, and its
token opens no other route on red5. Once per host, after the host has pulled
this code:

1. On red5, mint a jobs code (it prints the line to run on the host):

   ```
   media-visual-canvas pair --device "pn jobs" --jobs --server https://red5.sasonica.com
   ```

2. On the host, that line. It pairs, keeps the token in
   `~/.config/agent-media/jobs-worker.json` (0600), and installs and starts
   the worker for the host's init:

   ```
   media jobs pair 'sasonica://pair?server=…&code=…'
   ```

3. From red5:

   ```
   media jobs list
   media update --host pn --dry-run
   media restart-services --host pn --dry-run
   ```

NAME is the first word of the device's name (`pn` for `pn jobs`). `media
jobs status` on the host says whether it is paired and its worker installed;
`media-setup profile`'s `jobs` row keeps it installed (absent until paired).

| | systemd --user (Linux) | runit (Termux, `/etc/service`) | Windows |
|---|---|---|---|
| worker installed as | `agent-media-jobs-worker.service`, generated (`ExecStart=/bin/sh <checkout>/…/jobs-worker/run`), enabled --now | the template dir linked into the service dir | logon task `agent-media\jobs-worker` (`conhost --headless <python> jobs-worker.py --generic`; from task XML with restart-on-failure when the Sasonica package is installed, else a plain ONLOGON task) |
| restart jobs | the units the checkout owns (`agent-media*`, `am-*`) | the service dirs that link into the checkout | the tasks under `\Sasonica\` and `\agent-media\` (End, then Run); every task counts as possibly the worker's own, so a restart there is always detached (10 s) |
| update jobs | `git pull --ff-only`, then the restart | same | same, with a checkout, python and `git` on PATH. A binary install (`sasonica install`) has no checkout: `media jobs pair` refuses to install the worker there (use `sasonica update`) |
| drift check | unit copies | file copies | none: tasks hold no copies |

The Windows column is written against Task Scheduler's documented behaviour
and tested with schtasks faked; it has not been run on a Windows host yet.

**A reachable server: as a paired device of it.** For any other NAME, it
calls that host's `POST /services/restart` (server contract §6.6). The exit
code is the remote run's; 1 also covers not paired, not reachable, a refused
token, and a server too old to have the route.

A server that restarts itself always does it detached, 10 s later by
default, so its answer gets out before it is killed.

The credential is a device token for the other host's server, the same kind
the Sasonica app and the phone's Termux worker hold (contract §9). Pair once
per pair of hosts. On the host to be reached:

```
media-visual-canvas pair --device "red5 media"
```

Then on the asking host, with the `sasonica://pair?…` link it printed:

```
media peer pair homer 'sasonica://pair?server=…&code=…'
```

NAME is a `[peers.*]` alias in config.toml, a peer's `host`, or a role. The
token and the URL it reached are kept in
`~/.config/agent-media/peers/<alias>.json` (0600). `media peer list` shows
which peers are paired. With a bare code instead of a link, pass `--server
URL`; the default is `http://<peer host>:8781`.

## Applying to Other Packages

To drop this into `agent-sessions`, `agent-workspace`, or `pi-workspace`:
1. Pick a CLI command that gets polled frequently by tmux (or add a dedicated `pkg-status` script).
2. Wire in the `_skew_alert_line` function so it overrides the normal status output when an alert is active.
3. Adjust the `repos` list in the `doctor` function to include the specific repos that package cares about.