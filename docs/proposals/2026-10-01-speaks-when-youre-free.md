# Proposal: Sasonica speaks when you're free (1 Oct 2026)

Status: **step 1 built 2 Oct 2026** (server: `free.py`, `POST
/device/state`, `GET /free`, the gate in `submit`; server-contract §6.22).
**Step 2 built 2 Oct** (sasonica-app 3d7de7f, installed on p8a): `Holds`
reports call, voice and quiet on change and every 2 min while busy.
Meetings come from the phone's own calendar, not red5's khal, decided
1 Oct. `night` is not in step 1 (open question 3). The gate holds below
HIGH priority rather than by speech level: an interrupt-level reply is HIGH
already, and a quiet-level thread is left alone (David picked it from a list
of new directions). Adds one "can David be spoken to now?" answer on the
server, a device-state report from the phone, and one spoken catch-up when
the answer turns back to yes. A new §6.x in `server-contract.md`; one more
frame on `GET /sessions/events` (§6.13). No new app tab.

## What prompted it

Speech today is held for one reason at a time, each in its own place. A
call holds it on the phone. A silent ringer drops alerts on red5. A reply
nobody is looking at waits for a Play. None of them knows about a meeting,
and none of them ever says "here is what you missed". When David comes
out of a call or a meeting, the news is spread over the Alerts section,
unheard replies in several threads and silenced rows in history. He has to
go looking.

The idea: Sasonica knows from the ringer, calls, the calendar and the time
of day when he can be spoken to. Non-urgent news waits, then arrives as one
short spoken catch-up when he is free.

## What exists

**On the phone (sasonica-app).**

- `Holds.java` holds speech for a voice session or a phone call, read from
  the audio mode (`speech/Holds.java:224-228`). An `urgent` reply breaks
  through for the rest of the session (`:232`). After the session the held
  reply carries on (`:242-250`). This is the only place that knows about a
  call *and* plays speech.
- The **Speak now / Later** card (`Holds.java:274-303`, actions `:185-200`):
  Speak now releases the hold for the rest of the session; Later only drops
  the card.
- `RingerState.quiet()` (`speech/RingerState.java:130-136`): silent or
  vibrate, or DND while the policy grant is held. Unknown is never quiet.
- Readouts on loopback **:8774** (`speech/Readouts.java:34`): `/mic`,
  `/ringer`, `/state` (`:119-142`). Loopback only, so red5 cannot ask.
- `MpvServer` answers the ringer verdict itself as broker `user-data`
  (`speech/MpvServer.java:139`), so red5 reads it per alert without Termux.
- `NotifyService` holds `/sessions/events` all day and posts alerts on the
  "Alerts" channel, silently on a silent phone (`NotifyService.java:46-52`,
  `:101`). `NotifyRules` posts "New reply" when the app is off screen
  (`NotifyRules.java:210`).
- The manifest has no `READ_CALENDAR` or `READ_PHONE_STATE`
  (`AndroidManifest.xml:166-184`).

**In agent-media.**

- Priorities: `LOW`, `NORMAL`, `HIGH`, `URGENT` (`core/types.py:31-37`).
  `media say --urgent`, `--alert`, `--hold` (`core/cli.py:8826-8838`).
- Speech levels per conversation: interrupt, auto, pocket, normal, quiet
  (`core/speak_priority.py:1-19`, `:54-58`). A normal reply nobody is
  looking at is held unheard with a Play (`intake/hook_claude_code.py:1074-1088`,
  `intake/toast.py:1-19`, `should_hold` `:98`). The pocket lease keeps a
  locked thread "open" for 30 min (`core/watching.py:15-23`).
- The ringer gate: alert-class speech to the phone is **dropped** while the
  phone is quiet, and written to history as `extras.silenced = "ringer"`
  (`intake/submit.py:3325-3354`, `_record_silenced` `:3357-3398`, wired at
  `:3701` and `:5068`). Published from `ringer.py` every 20 s
  (`core/ringer.py:64-72`). The 28 Aug proposal chose drop over defer and
  "no clock anywhere" (`2026-08-28-silent-ringer-silences-alerts.md` §1, §5).
- `call_guard` on Termux pauses the Termux brokers on a ring and never
  resumes them (`core/call_guard.py:18-22`). It does not reach Sasonica's
  own player, which is why `Holds` exists.
- The alert store: raises and escalations become notices
  (`server/alerts.py:41-46`, `notices` `:438`); a digest's `spoken` is
  rendered held (`_render_held` `:196`). The agenda digest already reports
  this way and is never read out on its own
  (`~/agent-config/bin/agent-org-agenda-digest:44-49`).
- Priority-A agenda alarms are alert-class and expire after 10 min
  (`core/agenda_alarm.py:1-15`, `:32`).
- Summaries through the gateway: `_summary._chat` (`intake/_summary.py:106`)
  already writes recaps for rested threads (`server/reap.py:68`, `:242`,
  `generate_recap` `:453`); `recaps.recap_for` (`server/recaps.py:330`).

**Calendar.** Nothing in agent-media, Sasonica or agent-config reads one.
On red5, `vdirsyncer` and `khal` are installed and configured
(`~/.config/vdirsyncer/config`: Google CalDAV for davidj4phs, ryerorg,
davidj422; `~/.config/khal/config` adds davidj4x, davidj4tantra), but
`~/.calendars/` was last synced **4 Jul 2026** and no timer runs it. The
music-transit skill reads Google Calendar through the claude.ai connector
(`skills/music-transit/SKILL.md:127`), which only a Claude session has.

## The shape

[[visual: left, four source boxes stacked: "Phone: call / voice session (audio mode)", "Phone: ringer + DND", "Phone: calendar (busy event now)", "Server: quiet hours (optional)"; the three phone boxes join one arrow labelled "POST /device/state (on change)" into a central box "red5: free.py — free? why, until"; the server box feeds it too. From the central box two arrows: up to "Gate in submit: hold non-urgent, let urgent through" and right to "busy → free edge" which leads to "catch-up builder (held replies, alerts, silenced rows, digests) → one summary via gateway" then to "phone speaks one clip + 'While you were busy' card". A dashed bypass arrow from "urgent / needs" straight to "phone speaks now"]]

### 1. One answer: `free.py` on the server

Computed on red5, because every producer of non-urgent speech is there
(hooks, alert store, agenda alarms) and so is the gateway that writes the
summary. The phone supplies the facts only it can see.

`GET /free` (gated) → `{"free": false, "why": ["call"], "since", "until",
"held": 3}`. Reasons, any one makes David busy:

- `call`: a phone call or a voice session (the same test as
  `Holds.java:224-228`).
- `quiet`: `RingerState.quiet()` is true.
- `meeting`: a calendar event marked busy, not all-day, not declined, is
  under way. `until` is its end.
- `night`: optional quiet hours from Settings, off by default (the 28 Aug
  rule was "the ringer is the state"; see open questions).
- `manual`: David said "busy for an hour" or "not now".

**Fails open, as the ringer gate does.** No report from the phone for
5 min, or a report older than that, means free. A wrongly held catch-up is
silent by construction and looks like broken speech.

### 2. The phone reports on change

A new `POST /device/state {call, voice, quiet, mode, dnd, meeting_until,
meeting_title?}` with the device token, sent by the app when any field
changes and every 2 min while busy (a heartbeat, so a dead app reads as
free). Built from what `Holds` already reads; the calendar half is new:
`CalendarContract.Instances` for "now", behind a `READ_CALENDAR` grant.
The phone already syncs David's Google accounts, so no new credentials.

### 3. What is held, what breaks through

Held while busy (each still lands where it does today: transcript, Alerts,
history; only the voice waits):

- replies at **normal, pocket and auto** levels;
- alert-class speech (`--alert`), digests, agenda alarms. These keep being
  dropped by `_ringer_hold` and recorded; the catch-up reads the record.

Breaks through:

- `URGENT` speech (`media say --urgent`) and `needs` alerts;
- questions and permission prompts stay on "Needs you" (silent on a quiet
  phone, but always posted), never in the catch-up;
- an **interrupt**-level conversation (David marked it as one he wants
  heard), except during a call, where `Holds` keeps its rule.

During a call nothing changes on the phone: `Holds` still pauses the reply
in flight and plays it after. What changes is that red5 stops sending new
ones, so they go to the catch-up instead of queueing behind it.

### 4. The catch-up

One clip, not a replay. Built from what happened in the busy window:

- unheard held replies (`extras.held` and not `heard`), grouped by thread,
  from each thread's title and the reply's first lines;
- alert notices raised or escalated, and ones that cleared on their own;
- silenced alert rows (`extras.silenced`), with expired ones such as agenda
  alarms said as "missed", not as "now";
- new digests, by title only.

Written by `_summary._chat` with a catch-up prompt (the `generate_recap`
pattern), capped at about 120 words, about 45 s. Plain lines with counts:
"While you were in the 2 o'clock: the tests passed in agent-media, and the
radio thread wants a decision. red5's disk went to 93 % and came back. One
agenda alarm, call the bank, was missed." If the gateway fails, a template
says the counts and thread titles. Never silent.

Spoken to the phone as one `HIGH` event, `kind: catchup`, so it is in
history and replays like any clip. With it, a notification "While you were
busy · 2 replies, 1 alert" whose tap opens Home; each item keeps its own
Play. An item played from the catch-up's list is marked heard as today.

### 5. When it fires

On the busy → free edge, after a settle (call: 30 s; meeting: at its end
or when the next event does not follow within 10 min; DND or ringer off:
60 s). Only if something is held. Nothing held, nothing said.

- **Call ends.** `Holds` first finishes the reply it paused, then the
  catch-up.
- **Meeting ends.** The phone's next report clears `meeting`.
- **Morning.** The first time the phone leaves `quiet` after 05:00. The
  08:45 agenda digest joins the catch-up if it is waiting, so the morning
  is one clip, not two.

A catch-up that finds David busy again by the time it is written waits for
the next edge.

### 6. Overrides

- The existing card, extended: while held, "Holding 2 for later" with
  **Catch me up**, **Speak as it comes** (the old Speak now, for this busy
  spell) and **Later**.
- A Settings section, "When you're busy": calendar on/off and which
  calendars, quiet hours, the catch-up length.
- "Catch me up" by voice or from Home's header, any time: the same builder,
  on demand. `media catchup` at the desk.
- "Busy for an hour" / "I'm free" from the status notification, as
  `manual`.

## Order

1. **Server: `free.py`, `POST /device/state`, `GET /free`,** the gate in
   `submit` for non-urgent phone speech, fail-open tests. Contract §6.x.
2. **App: the report** from `Holds` (call, voice, quiet), no calendar yet.
   The call and ringer cases work end to end with a manual catch-up.
3. **Catch-up builder** on the edge, the template first, then the gateway
   summary; the notification.
4. **Calendar** on the phone, behind the grant, with a Diagnostics row.
5. **Morning and the agenda digest** joined; the Settings section and the
   extended card. A What's new line.

## Open questions

1. **Calendar source.** Phone `CalendarContract` (one grant, every account
   the phone syncs; recommended) or red5's `khal` with a `vdirsyncer` timer
   (configured, stale since 4 Jul, app passwords on red5)?
2. **The 28 Aug "drop, don't defer".** Alerts stay unspoken, but their
   record now feeds a summary. Is a summary of what was missed welcome, or
   should silenced alerts stay out of the catch-up?
3. **Night.** The ringer only, as decided on 28 Aug, or a quiet-hours
   setting too?
4. **Auto-level conversations.** Held in a meeting like normal ones, or
   do they always speak?
5. **Looking at the thread.** If David opens a thread mid-meeting, does its
   reply speak (he chose to look) or stay held (he may be reading in
   silence)?

## Verification

- `pytest packages/server/tests/test_free.py`: each reason alone, a stale
  report reads free, a missing phone reads free, `urgent` and `needs` pass
  the gate, the edge fires once and only with something held.
- `pytest packages/core/tests/test_ringer_gate.py` still green: the drop
  path is unchanged.
- App JUnit: the report's fields from fixed audio modes and ringer states;
  the calendar query from a fake cursor (busy, free, all-day, declined).
- On p8a (with David's go-ahead, and no test playback left behind): a call
  from another phone while a headless session replies → no speech in the
  call, the paused reply finishes after it, then one catch-up naming the
  thread. A 15-min test event marked busy → a reply held, one catch-up at
  its end. `media say --urgent` mid-event → spoken at once.
- `media doctor`: `free=`, `free_why=`, `free_age_s=`, `catchups_24h=`.
