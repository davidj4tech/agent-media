# Organiser: a calendar

David, 20 Oct 2026: "I was thinking grid plus Google calendar... And work
with other calendar services like Outlook? with a day, 3 day, week and month
view". Sources: both the server and the phone. Editing: create and edit
events, not only read them.

Implementation is partial: the Org grid and read-only server vdir/ICS
sources exist. Phone upload and Settings source management remain proposed.

Calendar selection and its relationship to automatic speech are specified
in [Calendar settings: what is shown and what holds speech](2026-10-21-calendar-selection-and-speech-holds.md)
(21 October 2026).

## What there is today

- The Organiser's Agenda (contract §6.10) lists Org headings scheduled or
  due within 7 days, plus overdue ones and timestamped events, grouped by
  day. Repeaters are not expanded, so a weekly routine shows up once.
- No calendar service is read for display. The app's shell reads the
  phone's calendar (`speech/CalendarRead.java`, `CalendarNow.java`,
  `READ_CALENDAR`) only for "Hold replies during meetings", which sends the
  current meeting, its end and its title.
- On red5, vdirsyncer and khal are configured for Google CalDAV
  (davidj4phs, ryerorg, davidj422 in `~/.config/vdirsyncer/config`; khal
  adds davidj4x and davidj4tantra), but `~/.calendars/` was last synced
  4 Jul 2026 and no timer runs it
  (proposals/2026-10-01-speaks-when-youre-free.md).

## Sources

Every event carries `source`, `calendar` (id, name, colour) and the
provider's `uid`. An event that arrives from more than one source (same
UID) is shown once, and the server's copy wins.

1. **CalDAV on the server** (Google, iCloud, Fastmail, Nextcloud).
   vdirsyncer runs on a timer (every 5 minutes) into `~/.calendars/`, and
   the server reads the .ics files there. It syncs both ways, so it also
   carries edits back to the provider.
2. **Microsoft Graph on the server** (Outlook.com, Microsoft 365).
   Outlook has no CalDAV for personal accounts. Graph needs a one-off app
   registration and a device-code sign-in, and the server keeps the refresh
   token (0600). A work tenant may need its admin's consent.
3. **ICS links on the server** (any service's "secret address"). Read
   only. The fallback for anything without 1 or 2.
4. **The phone's calendar** (any account on Android, including ones the
   server cannot reach). The app reads CalendarContract instances for the
   window on screen and posts them to `POST /calendar/phone`. The server
   keeps the last upload per device, so other clients (web, Pine Note) see
   them, as fresh as the phone's last visit. Only calendars the owner picks
   are sent.

## Server

- `GET /calendar?from=<date>&to=<date>` returns `{calendars: [...],
  items: [...]}`, merging events from all sources with the Org items in the
  same range: scheduled, deadline and timestamped headings, **with
  repeaters expanded inside the range**. Each item is `{kind: event|org,
  start, end?, all_day, title, location?, calendar, editable, ref}`. The
  existing `/org/view?name=agenda` stays as it is.
- `GET /calendar/sources`, plus add and remove, for Settings: which sources
  there are, their state and when each last synced. The Setup checklist
  gets a calendar row.
- Editing: `POST /calendar/events` to create, `PATCH` and `DELETE
  /calendar/events/<ref>` to change or remove. A change made since the app
  last read the event is a 409 with both versions, as contact edits are
  (§6.25). Where each write goes:
  - CalDAV: write the .ics into the vdirsyncer folder and run a sync at once.
  - Graph: the Graph events API.
  - A phone-only calendar: the server queues the change, and the app
    applies it through CalendarContract (`WRITE_CALENDAR`) on its next
    visit, after which Android's own sync carries it to the provider.
  - ICS links: read only (`editable: false`).
  - Org items: moving one rewrites its SCHEDULED or DEADLINE line through
    the existing Org write path.

## App

- Views: **Day, 3 days, Week, Month**, picked from a strip like the
  Organiser's views. The view and the date are remembered.
- Day, 3 days and Week are time grids, with an all-day row on top. Month is
  a grid with up to three lines per day and "+n" for the rest. Swipe for the
  next or previous range, and Today to jump back.
- Org items sit in the grid with their state keyword. A deadline is marked
  as a deadline, and ticking one marks it done (useMarkDone).
- Tap an empty slot to create an event (calendar, title, time, place). Tap
  an event for its detail, Edit and Delete. Long-press and drag to move an
  event is a later step.
- Calendar colours on a phone. On the Pine Note (DU4, 4 greys): outlines
  and text only, calendars told apart by line style, no fills.

## For everyone, not one setup

David, 20 Oct 2026, agreed: every step is built for any install, red5 being
only the first. So:

- Sources are per server, in config.toml (`[[calendar.sources]]`), added
  and removed from the app's Settings, never written into the code. red5's
  vdirsyncer folders are one source among others.
- A folder of .ics files (a vdir, as vdirsyncer, khal and others keep) and
  an ICS link need no sign-in from us, so they come first. Parsing is the
  `icalendar` library (BSD) and repeats `recurring-ical-events` (LGPL,
  imported as a library, never copied in; check against the licensing
  decision before adding it).
- Google and Microsoft sign-in need a registered app. A public release
  needs one each, held by South Pen Labs, and Google's review of the
  calendar scope before strangers can use it. Until then, and for anyone
  self-hosting, the server takes the install's own client id from config.
- Nothing here assumes the tailnet (docs/decisions/2026-10-18-off-the-tailnet.md).

## Order

1. `GET /calendar` with Org items only (repeaters expanded), plus the four
   views in the app. Useful on its own, with no accounts. DONE 20 Oct 2026.
2. Sources in config, and the first two kinds: a vdir folder and an ICS
   link, merged into `GET /calendar`; on red5, vdirsyncer on a timer again
   and its folders added as sources.
3. The phone's calendar upload.
5. Create and edit through CalDAV and the phone. Moving Org items.
6. Microsoft Graph, read and write.

**Shared with mail** (20 Oct 2026): the Microsoft Graph sign-in here also serves Outlook mail — docs/proposals/2026-10-20-mail-in-sasonica.md.

## Open questions

- Which Google calendars: vdirsyncer's three, or khal's five?
- Is Outlook a personal account or a work tenant? If a tenant, will its
  admin allow the app?
- Does the old Agenda list stay as its own view, or become the Month view's
  day list?
