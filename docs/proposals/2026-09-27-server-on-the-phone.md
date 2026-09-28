# The server on the phone: Sasonica with nothing else to set up

(Any Android device, not only a phone: a tablet, a TV box or a Chromebook
runs Termux just as well. The app says "this device", the installer is
`deploy/android/install.sh`, and it pairs under the device's model name.
David, 27 Sep 2026.)

Status: proposal, nothing built.
Date: 2026-09-27

David, after the welcome screen landed (sasonica-app `afff1c2`): *"something
else that doesn't require pairing first. could be to install on the
localhost... which I guess would require a termux install"*, and then:
*"localhost as a base could also include some instructions to turn on
developer mode and ADB which would make it quite powerful right from the
get-go"*.

## Recommendation in one line

A third way in on the welcome screen, **Run it on this device**. The app looks
for a server on `127.0.0.1:8781`. If there is none, it walks the user through
Termux and a one-line installer. The installer ends by handing the app a
pairing link, so pairing happens without typing. An optional **ADB power-up**
keeps Android from killing the server and grants what Settings would
otherwise need several visits to grant.

## What p8a already proves (checked 27 Sep 2026, read-only)

This is not new ground. David's phone has run agent-media under Termux for
months:

- Termux runit services: `canvas-local` among ~40 others. The canvas answers
  `http://127.0.0.1:8781/healthz` with 200.
- Agents that run natively: Codex (`codex-cli 0.121.0`) and pi (`0.81.1`),
  both Node launchers, plus tmux, Python 3.14, Node 26 and `adb`
  (android-tools).
- **Claude Code and opencode do not.** Their binaries are built for glibc, not
  Android's Bionic. `~/bin/claude` is a wrapper that runs Claude Code in a
  Debian proot, and opencode runs in a proot Arch (TermuxArch). Corrected
  27 Sep 2026: the first draft listed Claude Code as native.
- `allow-external-apps = true` is set in `~/.termux/termux.properties`, so
  another app can already run commands in Termux (the `RUN_COMMAND` intent).
- ADB over loopback works: Termux's own adb pairs to `127.0.0.1`. It is how
  we deploy today (`agent-phone-adb`, memory `adb-shell-via-self-pairing`).
- Android 17.

What is missing is a path someone else could follow. Today it is our setup,
not an install.

## Where the server lives: a Debian proot (tested 27 Sep 2026)

In plain Termux the server's Python dependencies don't install:
`pydantic-core` and `rpds-py` (via `mcp`) have no Android wheels, and
building them needs Termux's Rust (131 MB download, 596 MB installed), then a
long compile. p8a has Rust for exactly that reason. Termux ships
`python-rpds-py`, but no `pydantic-core`.

Since Claude Code and opencode need a glibc proot anyway, the server lives
there too. **Termux is only the host:** runit, `am`, adb. A Debian
proot-distro holds agent-media and the agents, and every Python package
comes as a ready wheel. Debian plus the server is ~425 MB.

`deploy/android/install.sh` does this. Run in a clean Termux (the
`termux/termux-docker` image under podman on red5), it:
- installed Debian, agent-media and opencode;
- started the canvas on 127.0.0.1:8781 and sessiond, as Termux runit
  services that log into Debian;
- minted a `sasonica://pair` link, whose code `POST /pair` redeemed;
- took an `/ask` with `agent: opencode`, which started a real opencode
  session in tmux. Its reply was not read back.

**opencode is the default agent** (David, 27 Sep 2026): its free models need
no sign-in or paid plan. Claude Code is an option (`SASONICA_AGENTS="opencode
claude"`). The Sasonica Shell runner is plain Node, so it can run on the
phone too. Then the claude.ai connector reaches the phone from any chat app,
with no tailnet.

## The flow

1. **Welcome → Run it on this device.** The app probes
   `http://127.0.0.1:8781/healthz`. If a server answers, the app skips to
   step 4 and asks that server for a code (below).
2. **No server: install Termux.** The app explains that Termux comes from
   F-Droid or GitHub, because the Play Store build is years stale, and links
   there. We cannot install it for them.
3. **One line, pasted into Termux.** The app shows it with a Copy key:
   `curl -fsSL https://sasonica.com/install | bash` (live 28 Sep 2026: `deploy/install.sh` sees Termux and runs `deploy/android/install.sh`; `sasonica.com/android` goes straight there). The
   installer:
   - installs `proot-distro` and `termux-services`, then Debian with
     Python, git, tmux and Node;
   - clones agent-media into Debian's `~/projects/agent-media` (a venv) and
     installs opencode;
   - writes two Termux runit services, `sasonica-canvas` (loopback :8781)
     and `sasonica-sessiond`, each started inside Debian;
   - sets `allow-external-apps = true` so the app can drive Termux from then
     on;
   - takes `termux-wake-lock`.
4. **The handoff: pairing with no typing.** The installer mints a code
   (`media-visual-canvas pair --device "<the model, e.g. Pixel 8a>" --host 127.0.0.1`) and
   opens `sasonica://pair?server=http://127.0.0.1:8781&code=…` with
   `am start` (which works from the Termux uid). The app pairs as it would
   from a pasted link.
   - **Needs in the app:** a `VIEW` intent filter for `sasonica://pair`. The
     manifest has none today; `parsePairLink` already reads the link.
5. **An agent.** The Coding agents page (§6.6) already installs and signs in
   harnesses. Pointed at the phone's own server, it installs Codex or pi
   into Termux directly. Claude Code and opencode need a proot distro first
   (`proot-distro install debian`, a few hundred MB) and a wrapper like
   p8a's `~/bin/claude`. That is heavier, but it is the agent most people
   will ask for, so the installer should offer it as its own step, not
   leave it out.

**Why not simply trust loopback?** Every app on the phone can reach
`127.0.0.1`. A server that paired anything asking from loopback would hand a
token to any app that tried. The installer's code is the proof, and it never
leaves the device.

## Staying alive

Android kills Termux in three ways, and a newcomer will not know any of
them:

| Killer | Without ADB | With ADB |
| --- | --- | --- |
| Battery optimisation | the app opens Termux's battery page and says what to tap | `dumpsys deviceidle whitelist +com.termux` |
| The phantom-process killer (Android 12+) | Developer options → *Disable child process restrictions* (Android 14+, where the ROM has it) | `settings put global settings_enable_monitor_phantom_procs false` |
| Doze with the screen off | `termux-wake-lock` (installer), plus a notification that stays | same |

The app should check each one and show which are still open, as
`media-setup status --json` does for a desk machine. That is the same pattern
as the "One setup for a machine" page (roadmap item 3).

## The ADB power-up (optional, David's idea)

Turned on once, ADB gives shell-uid powers that no app has by itself:

- the two killers above, fixed for good;
- `pm grant` and `appops` for Sasonica: Do Not Disturb access, the
  notification listener, `WRITE_SECURE_SETTINGS`, with no trips through
  Settings;
- the assistant role (`cmd role add-role-holder android.app.role.ASSISTANT
  com.sasonica.app`, done on p8a on 9 Sep);
- for agents working on the phone itself: `logcat`, `dumpsys`, `screencap`,
  and installing APKs.

**Pairing without the race we fought on p8a.** In August it took a screenshot,
OCR and a script to beat the pairing dialog's rotating code and port. Termux's
adb has no mDNS, and nothing on the phone reports the connect port. The app
can do better, as Shizuku does:

- Android's `NsdManager` *does* see `_adb-tls-pairing._tcp` and
  `_adb-tls-connect._tcp`. So the app knows both ports, including after the
  connect port moves.
- The app posts a notification with an inline reply field ("Enter the
  pairing code"). The user reads the code off the Wireless debugging dialog
  and types it in the notification without leaving the dialog.
- The app hands the port and code to Termux (`RUN_COMMAND`:
  `adb pair 127.0.0.1:<port> <code>`, then `adb connect`).

An alternative is to depend on **Shizuku** itself: mature, well known, with
an API for exactly this. It is one more app to install. Our own flow reuses
the Termux adb that is already there. My lean: our own flow, with Shizuku
kept as the fallback if `NsdManager` proves unreliable on some ROMs.

**What to tell people first.** These go on the power-up's own screen, before
anything is switched on:

- Developer options being on makes **some banking and payment apps refuse to
  run**. Say so before they turn it on.
- Wireless debugging is tied to the Wi-Fi network and switches off when it
  changes. It should not be enabled on a network the user does not trust
  (David's own rule, memory `adb-shell-via-self-pairing`, 30 Aug).
- Grants and settings made through ADB outlast it. The fixes can be applied
  once and Wireless debugging switched off again. (Check the phantom-process
  setting survives a reboot on Android 17.)
- **ADB is the whole phone.** The server should use it for a short, named
  list of actions (the grants and fixes above). Raw `adb shell` for agents is
  a separate switch, off by default, whose description says what it means.

## What changes, and what does not

- The agents work on the **phone's** files (`~/projects` in Termux), not a
  computer's. That suits someone trying Sasonica out or working from the
  phone alone. Most people will want a computer later.
- The app pairs with **one server at a time** today. Adding a computer later
  means pairing again, which drops the phone server's threads from view.
  Several servers at once is its own question, not part of this.
- Speech already renders on the phone (PhoneVoice), so a phone server gives
  up nothing there.
- Nothing here needs the tailnet. It pairs with the relay work in contract
  §19 as the other half of "no Tailscale".

## The order, if we do it

1. **The installer**, written from p8a's working setup and tested on a clean
   Termux: a second phone, or a wiped Termux on an emulator in CI. p8a's
   setup grew by hand, so this step finds what is only there by accident.
2. **The app:** the third welcome button, the localhost probe, the Termux
   steps, and the `sasonica://pair` intent filter.
   **Done 28 Sep 2026** (sasonica-app `c8be1d2` the link, then Run it on
   this device: routes/device.tsx; the install line fetches the script from
   GitHub's raw URL until it has a home of its own).
3. **Staying alive:** the checks and the Settings shortcuts, without ADB.
   **Done 28 Sep 2026:** Run it on this device → Keep it running (battery,
   Termux:Boot, the child-process limit; DeviceServerPlugin), and the
   installer writes `~/.termux/boot/sasonica` for Termux:Boot to run.
4. **The ADB power-up:** `NsdManager` discovery, the notification-reply
   pairing, `RUN_COMMAND`, and the named actions.
5. **Agents on the phone server:** the Coding agents page against it.
   Codex and pi natively; Claude Code and opencode through a proot distro,
   with the wrapper written from p8a's.

## Open questions

- Where the installer lives: settled, `sasonica.com/install` (David, 28 Sep
  2026). Pip or a git clone for agent-media on the phone: a clone, for now.
- Battery: what an idle canvas plus runit costs over a day, measured on p8a
  before we promise anything.
- Store policy: an app whose setup sends people to Termux on F-Droid is
  allowed on Play. Where the app ships at all is parked with monetization
  (roadmap item 4).
- Does the phone-role profile need Mopidy and Snapcast? Probably not: a
  newcomer's phone server is chat and speech, and music is a desk-machine
  extra.
