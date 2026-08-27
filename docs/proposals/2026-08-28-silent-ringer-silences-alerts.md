# When the phone is on silent, alerts don't speak

**Status:** implemented 2026-08-28, except the sideload (see §7).

## The complaint

`agent-org-agenda-digest.timer` fires at 08:45 and the phone talks, whether or
not the phone is meant to be making noise. The chain is:

```
agent-org-agenda-digest.timer (systemd --user, red5)
  └─ ~/.local/bin/agent-org-agenda-digest
      └─ agent-digest-pane pop --spoken …
          └─ _say()  →  media say "<ping>"
              └─ submit_event(Event(source=CLI, priority=NORMAL))
                  └─ render on red5 → sink-speech → tcp://100.94.14.59:6602
```

Everything after `media say` is this repo. The two scripts before it are in
`~/agent-config/bin`; they need one flag added (§4).

## 1. Drop, don't defer — so not call_guard

`call_guard`'s hold *pauses* the broker and, on the flag/mic path,
*auto-resumes* when the hold lifts. A night of silenced alerts would queue and
then all play at once at breakfast. An alert that missed its moment is not an
alert any more; it is noise with a timestamp. So: dropped.

The precedent that already has the right semantics is three lines away from
where this belongs — `submit.py:2847`, the durable per-pane mute:

> a muted pane still renders its clips and records a replayable history row,
> but is never played through the broker and never ducks music.

That is exactly the behaviour wanted. Rendered, recorded, replayable from the
popup or `media history`, and silent. The digest's tmux pane still shows the
agenda; only the audio is withheld.

## 2. Where the gate goes

`submit_event`, beside `muted`, before the `remote_say` branch so both the
local-render and remote-render lanes are covered:

```python
silenced = _ringer_silenced(target, event)     # new
muted = state.resolve_mute(source_pane, source_tmux_session)
if muted or silenced:
    playback_lock.release()
```

Scoped to the target. The phone's ringer says nothing about the lounge
speakers, so the gate fires only for the target the ringer source is bound to
(`MEDIA_RINGER_TARGET`, default `phone`). A `--target local` reply, or rooms
playback, is untouched.

**Fails open, always.** Unreadable ringer state, missing service, stale
snapshot, phone unreachable → speak. Silencing on ignorance turns every
transient into "TTS is broken", which is the exact bug class this codebase
keeps paying for (mic-block reverts, media volume 0/25). Only a fresh, positive
"the ringer is off" suppresses anything.

**Logged, every time.** A dropped alert writes a history row plus a
`state.log_error`-style breadcrumb, and `media doctor` grows a fact:
`alerts_silenced_24h=3`. Otherwise "my morning digest stopped talking" and "TTS
is broken" look identical from the outside — and we have twice diagnosed a
healthy stack because a silent-by-design behaviour left no trace.

## 3. Reading the ringer

**Decided 2026-08-28: the companion app answers it.** A new `/ringer` on
`StatusServer`, mirroring `/mic` — same server, same loopback bind, same
`Source` interface with a `default` so every test implementor is unaffected.

```java
/** What the phone's ringer is doing — one line, first field the mode. */
default String ringer() { return "normal (no probe)"; }
```

backed in `CompanionService` by `AudioManager.getRingerMode()` →
`silent` | `vibrate` | `normal`. Cheap enough to read per request; no
broadcast receiver needed unless we later want to log transitions.

### Do Not Disturb, and why to answer it in the same breath

`getRingerMode()` is the *ringer switch*. On modern Android, Do Not Disturb
does **not** move it — a phone in DND can report `normal` while being, in every
sense David means, on silent. The one API that answers DND is
`NotificationManager.getCurrentInterruptionFilter()`, and it needs
`ACCESS_NOTIFICATION_POLICY`. That is a user grant through
`ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS` — **not** the notification-listener
access that Play Protect blocks on sideloads, so it is actually obtainable here.

Since the APK is being rebuilt anyway, the marginal cost of reporting both is a
few lines, and the cost of *not* is a gate that silently misses the way David
may well be silencing the phone. So `/ringer` answers both:

```
GET /ringer -> "silent dnd=priority granted=1"
               "normal dnd=all granted=1"
               "normal dnd=unknown granted=0"      # no policy grant yet
```

Policy: **quiet if the ringer mode is silent-or-vibrate, OR the interruption
filter is anything but `all` while the grant is held.** An ungranted filter is
`unknown` and contributes nothing — it never silences on an unanswered
question. If the grant proves awkward to obtain, the mode field alone still
gates and the DND half degrades to off.

### Getting the answer to red5

`/ringer` is bound to 127.0.0.1 like the rest of port 8770, and deliberately —
so the phone still needs to publish. Modelled on `mic_block.py`, the existing
service that holds a fact nobody else can read:

* new `ringer-state` runit service on p8a, ~120 lines: poll
  `127.0.0.1:8770/ringer` every N seconds, publish
  `state_dir()/ringer.json` (`{mode, dnd, granted, checked_at}`) for
  `media doctor`, **and** write the decided quiet/loud verdict into the speech
  broker's mpv `user-data` as `user-data/agent-media/ringer`.

`user-data` on the broker is the channel this codebase already uses for exactly
this problem — a fact every host must agree on, published on the socket they
all already talk to (`_BROKER_OWNER_KEY`: *"stored in mpv `user-data` on the
broker itself, so all hosts see the same value"*). No new port, no ssh in the
say path, nothing for red5 to learn. An mpv too old to have it, or a phone with
the service not installed, reads as absent → speak.

Each read is one bridge round-trip (~600 ms per the batching note in
`play_playlist`). **That cost lands only on alert-class speech**, which is the
narrow scope earning its keep: a conversational reply never pays it. The
published value carries `checked_at`; older than `MEDIA_RINGER_MAX_AGE_S`
(default 300) is treated as unknown → speak.

Reviving the app is already `MicSource`'s job, and it revives the whole
process, so `/ringer` inherits that liveness for free.

## 5. What this is not

* Not quiet hours — no clock anywhere in this. The phone's ringer is the state,
  and David already sets it deliberately every night.
* Not a mute of the *canvas* or the tmux pane. Text is silent already.
* Not `--urgent`'s business. An urgent alert on a silenced phone is still
  silenced; if a genuine can't-miss tier is ever wanted, that is a separate
  `MEDIA_RINGER_ALWAYS_SPEAK_PRIORITY` knob and not part of this.

## 6. Scope — settled 2026-08-28

**Alert-class only.** Gate `metadata["alert"]` speech; everything else speaks
whatever the ringer says. A reply asked for mid-conversation comes through.

The accepted cost: every alert-class producer must be taught the flag, or it
keeps talking. Today that is two scripts. When a third appears and nobody
remembers, it will speak at 3am once before anyone notices — which is the right
way round, because the other failure (a wanted reply silently swallowed) is the
one that costs an hour to diagnose.

Rejected: gating all phone-targeted TTS. It makes a wrong ringer read total
rather than partial, makes every reply pay the round-trip, and turns "I asked
for that" into a `media history` archaeology exercise.

## 7. What shipped

Host side, all landed and tested:

* `agent_media_core/ringer.py` — poll `/ringer`, decide, publish to
  `state_dir()/ringer.json` **and** to the speech broker's `user-data`.
* `services/ringer-state/` — runit service, `requires: observe`; console script
  `media-ringer`.
* `sinks/speech.py` — `RINGER_PROPERTY`, `set_ringer`, `read_ringer`
  (`RINGER_MAX_AGE_S = 300`, and None for every unknown).
* `intake/submit.py` — `_ringer_hold` / `_record_silenced`, wired into both
  `submit_event` and `submit_stream` ahead of the remote-say branch.
* `cli.py` — `media say --alert`; `_ringer_facts()` in `media doctor`
  (`ringer=`, `ringer_mode=`, `ringer_dnd=`, `ringer_age_s=`,
  `alerts_held_24h=`).
* `~/agent-config/bin/agent-digest-pane` — `_say "$text" alert`, on the ping
  and the active digest but never the ↵ read.
* `tests/test_ringer_gate.py` — 26 checks, most of them fail-open cases.

App side, written and host-tested, **not yet on the phone**:

* `RingerState.java` (pure Java, host-testable) + `RingerTest` — 15 checks.
* `StatusServer` `/ringer` route; `CompanionService.ringer()`;
  `ACCESS_NOTIFICATION_POLICY` in the manifest; a **Diagnostics → Do Not
  Disturb** row that shows the grant and opens the settings screen;
  `FocusControl.assertConstantsMatch` extended to the ringer modes and
  interruption filters, because a silently wrong constant here does not crash —
  it withholds somebody's morning alerts.

### One thing worth knowing

The publisher writes to its **local** broker and the origin reads the **phone**
target. In production these are the same mpv — the tcp bridge is what makes
them two names — but it is the kind of identity that reads as a bug in a test
harness, and did once while this was being built.

### Left to do

1. Build the APK from a throwaway worktree (so it is not stamped `+dirty`) and
   sideload to p8a.
2. Grant Do Not Disturb access from the Diagnostics screen.
3. `media-setup install-services` on p8a to pick up `ringer-state`.
4. Confirm on the device: `curl 127.0.0.1:8770/ringer` with the phone silent,
   then normal, then in DND at ringer-normal — the third is the case the whole
   two-field design exists for, and the only one that cannot be checked here.
