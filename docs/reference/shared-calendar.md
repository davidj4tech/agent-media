# Shared calendar access

All trusted local Sasonica harnesses can read the owner's calendars through
the same command, independently of OpenAI's account connector:

```sh
media calendar --start 2026-10-27 --end 2026-11-10
media calendar --search Marlo
```

The command returns JSON with events, calendar names, busy/free flags and
source status. Dates are inclusive and use the server's local timezone.
Without dates it reads today through six days later. A source's `synced`
value for a local folder is its newest event modification, **not** proof
of a successful remote sync. Check the existing `vdirsyncer.service` for
remote-sync health. Missing sources or a failed sync do not prove availability.

`calendar_events(start, end, search)` exposes the same query through the
existing `media-mcp` and `media-mcp-narrow` stdio servers. Reconnect an
existing MCP client to discover it. Harnesses without MCP use the CLI.
The unauthenticated legacy HTTP MCP transport excludes this private tool.
Configured feed URLs and filesystem paths are never returned in the response.

Calendar writes use the host's existing CalDAV connection and the configured
`calendar.write_pair` in agent-media's config. `media calendar put --event
FILE.json` creates an event; repeat a stable UID to retrieve its current ETag.
Supply `--etag` to update it. `media calendar delete --event FILE.json --etag
ETAG` removes it, where the JSON contains its UID. Only Sasonica-managed events
can be changed. `calendar_write` exposes the same operation over local stdio
MCP, and is also excluded from the unauthenticated HTTP transport.

An event contains `title`, `start`, `end` (ISO datetimes with explicit offsets),
optional `uid`, `timezone`, `description`, `location`, `busy`, `rrule` and
`all_day`. All-day dates use an exclusive end. Keep stable UIDs for automation.
Updates use conditional CalDAV requests and read-back; conflicts are errors.
No app update or OpenAI connector is required.

## Booking availability

`calendar-availability-sync.timer` publishes every ten minutes to Rye Remedial
over authenticated public HTTPS. Its owner-only configuration is
`~/.config/agent-media/calendar-publish.json`; the matching scoped token lives
in Drupal State and is never exported to Git. The payload contains only busy
intervals and designated shift end plus one hour, not event titles, locations,
client identities or source URLs. Every publication requires a successful
vdirsyncer run. New bookings close if the last verified snapshot exceeds two
hours; existing bookings stay in place, and conflicts raise an alert.

Marlo's fortnightly Tuesday shift is anchored on 27 October 2026, 09:45–13:45
Melbourne. Its first permissible Rye start is 14:45, subject to normal booking
notice, location, travel and other busy events. Confirmed shift openings
replace that day's static evening opening; unknown days retain the fallback.

`calendar-treatment-sync.timer` reads today and tomorrow's PHS staff timetable
hourly, using the existing encrypted staff login in a temporary private
session directory. It writes only David's treatment times. It never fetches
consultation forms, and only cancels its own future events after complete
source reads. These intervals do not establish the designated shift finish.

Employment Hero roster access is still awaiting an interactive login or a
verified roster subscription. No published PHS shift hours are inferred from
treatment times. Automatic conversion of a PHS shift into a free work-hours
context plus busy treatment intervals also remains pending that roster source.
