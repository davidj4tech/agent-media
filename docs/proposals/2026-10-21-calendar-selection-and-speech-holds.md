# Calendar settings: what is shown and what holds speech

Status: proposed, 21 October 2026. No implementation in this change.

David asked why a calendar affecting automatic speech was absent from the
Organiser, whether calendar selection is configurable, and requested a
proposal if it is not already implemented.

## The problem observed

The phone reported a busy event, “Spa Therapy”, ending 27 October. Both Pi
and Codex replies were saved with `held_why: busy`, although the conversation
was open. The event was not visible in the app's calendar view. Enabling
the existing “Speak as it comes” override restored permission to speak for
that busy spell. We have not established which source calendar owns the
event or whether its dates or busy status are correct.

Two independent paths explain the mismatch:

- Android's `speech/CalendarRead.java` reads instances from every visible
  phone calendar when “Hold replies during meetings” is on. There is no
  calendar selection. `CalendarNow.Ev.counts()` excludes all-day, free and
  declined events, but a timed event spanning several days can still count.
- The Organiser's `GET /calendar` merges Org items with server-configured
  vdir and ICS sources (`calendar_sources.py`). Phone calendar upload and
  source management in Settings are not implemented in the inspected code.

The existing [Organiser calendar proposal](2026-10-20-organiser-calendar.md)
already calls for Settings source management and uploading only calendars
the owner picks. This proposal defines the missing selection and speech
behaviour; it does not replace that broader work.

## Settings → Calendars

List calendars grouped by source/account, with name, colour, source type,
permission state and last successful sync or upload. Distinguish phone
calendars from this server's calendars. Give unavailable sources a reason
and a retry or permission action. Never expose secret ICS addresses.

Each calendar has two independent choices:

- **Show in Organiser**: include its events in the calendar view. For a
  phone calendar, explain before enabling that its events will be uploaded
  to the selected server and available to the owner's other clients.
- **Hold replies during busy events**: use this calendar to decide when
  automatic speech should wait on this phone. Explain that hiding a calendar
  from the Organiser does not disable speech holds.

Keep the existing master “Hold replies during meetings” switch, with a link
to these calendar choices. A display-only calendar never starts holding
speech by implication. Phone uploads are opt-in; granting Android calendar
permission alone does not upload events. Server source management remains
owner-only and follows the existing owner visibility rules.

Initially the hold selector applies to phone calendars, matching today's
implementation. Mark server-only calendars as display-only until server
availability evaluation exists; do not offer a control that does nothing.
Use “Add calendar source” for the vdir/ICS setup described in the parent
proposal; Google and Microsoft sign-in remain that proposal's later work.

## When speech is held

Show a persistent, compact explanation in the thread and speech controls:
“Replies are waiting: Spa Therapy · Work calendar · until Tue 27 Oct, 5 pm”.
Offer **Speak as it comes**, **Calendar settings**, and the event's details
when available. Show it even if that calendar is hidden from the Organiser.
Explain the scope of Speak as it comes: the current busy spell, after which
normal holding resumes. Do not automatically play a backlog when settings
change; offer the existing catch-up or individual Play controls.

Add **Hold during events longer than 24 hours**, off by default, to avoid a
timed holiday, rota or multi-day booking silently suppressing a week of
replies. Keep ignoring all-day, free and declined events. Explain skipped
long events in event details. Do not infer busy status from event titles.
Call, microphone, ringer and manual busy rules keep their existing controls.

## State and migration

Calendar selection uses source-scoped calendar IDs, never display names.
Android IDs are scoped to the device; account removal or resync can change
them. Flag a missing selection and ask for a replacement rather than
silently selecting all calendars. New calendars start unselected.

Store phone hold preferences on the phone. Store phone upload consent per
destination server, separately from local display filters. Keep server
source configuration on the server; the app renders the source catalogue
returned by it, following the generic-app design rule. Deduplicate events
across sources using the parent proposal's UID rules while retaining source
provenance for the hold explanation.

For existing installs, preserve the master hold switch and initialise the
hold selection from currently visible phone calendars if it was enabled.
Show a one-time review prompt describing that selection and the new long
event rule. Do not enable phone uploads during migration.

Extend the device busy report with safe source identity and the responsible
event's start/end and all-day status, enough to explain the hold. Keep IDs
scoped, omit credentials and avoid logging event details. Recompute and
report immediately after selection, permission or event changes, including
clearing a hold when its source is deselected. Existing clients without the
new fields keep the current server contract.

## Delivery and acceptance

1. Phone calendar catalogue and per-calendar hold selection, long-event
   rule, and visible hold explanation. This resolves unexpected silence
   without waiting for calendar upload.
2. Organiser selection and explicit phone upload consent, alongside source
   status and server source management from the parent proposal.
3. Unified event detail links and source provenance across both paths.

Verify with synthetic calendars and mocked device reports, not playback on
a real phone: independent display/hold selections; a hidden calendar's
hold explanation; same-name calendars; all-day/free/declined and multi-day
events; revoked permission; removed calendars; stale device reports;
selection changes clearing a hold; and upload consent per server. Check
that Pi and Codex replies follow the same decision and that neither source
configuration nor calendar data leaks to another paired account.
