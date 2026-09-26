# The server on the phone: Sasonica with nothing else to set up

Status: proposal, nothing built.
Date: 2026-09-27

David, after the welcome screen landed (sasonica-app `afff1c2`): *"something
else that doesn't require pairing first. could be to install on the
localhost... which I guess would require a termux install"*, and then:
*"localhost as a base could also include some instructions to turn on
developer mode and ADB which would make it quite powerful right from the
get-go"*.

## Recommendation in one line

A third way in on the welcome screen, **Run it on this phone**. The app looks
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
- Agents present natively: Claude Code, Codex (`codex-cli 0.121.0`) and pi
  (`0.81.1`), plus tmux, Python 3.14, Node 26 and `adb` (android-tools).
- **opencode is the exception.** It runs only inside a proot Arch (TermuxArch),
  because its binary is not built for Bionic.
- `allow-external-apps = true` is set in `~/.termux/termux.properties`, so
  another app can already run commands in Termux (the `RUN_COMMAND` intent).
- ADB over loopback works: Termux's own adb pairs to `127.0.0.1`. It is how
  we deploy today (`agent-phone-adb`, memory `adb-shell-via-self-pairing`).
- Android 17.

What is missing is a path someone else could follow. Today it is our setup,
not an install.

## The flow

1. **Welcome → Run it on this phone.** The app probes
   `http://127.0.0.1:8781/healthz`. If a server answers, the app skips to
   step 4 and asks that server for a code (below).
2. **No server: install Termux.** The app explains that Termux comes from
   F-Droid or GitHub, because the Play Store build is years stale, and links
   there. We cannot install it for them.
3. **One line, pasted into Termux.** The app shows it with a Copy key:
   `curl -fsSL https://sasonica.com/phone | bash` (host to be decided). The
   installer:
   - runs `pkg install python tmux nodejs git termux-services android-tools`
     (native wheels come from `pkg`, memory `termux-native-wheels-from-pkg`);
   - installs agent-media and runs `media-setup profile` with a phone role:
     the canvas, speech and the hooks, as runit services;
   - sets `allow-external-apps = true` so the app can drive Termux from then
     on;
   - takes `termux-wake-lock`.
4. **The handoff: pairing with no typing.** The installer mints a code
   (`media-visual-canvas pair --device "This phone" --host 127.0.0.1`) and
   opens `sasonica://pair?server=http://127.0.0.1:8781&code=…` with
   `am start` (which works from the Termux uid). The app pairs as it would
   from a pasted link.
   - **Needs in the app:** a `VIEW` intent filter for `sasonica://pair`. The
     manifest has none today; `parsePairLink` already reads the link.
5. **An agent.** The Coding agents page (§6.6) already installs and signs in
   harnesses. Pointed at the phone's own server, it installs Claude Code,
   Codex or pi into Termux. opencode waits until it runs without proot.

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
3. **Staying alive:** the checks and the Settings shortcuts, without ADB.
4. **The ADB power-up:** `NsdManager` discovery, the notification-reply
   pairing, `RUN_COMMAND`, and the named actions.
5. **Agents on the phone server:** the Coding agents page against it;
   opencode once it runs outside proot.

## Open questions

- Where the installer lives: `sasonica.com/phone`? And pip or a git clone for
  agent-media on the phone?
- Battery: what an idle canvas plus runit costs over a day, measured on p8a
  before we promise anything.
- Store policy: an app whose setup sends people to Termux on F-Droid is
  allowed on Play. Where the app ships at all is parked with monetization
  (roadmap item 4).
- Does the phone-role profile need Mopidy and Snapcast? Probably not: a
  newcomer's phone server is chat and speech, and music is a desk-machine
  extra.
