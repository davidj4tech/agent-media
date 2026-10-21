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
intervals and designated shift end plus 75 minutes, not event titles, locations,
client identities or source URLs. Every publication requires a successful
vdirsyncer run. New bookings close if the last verified snapshot exceeds two
hours; existing bookings stay in place, and conflicts raise an alert.

Marlo's fortnightly Tuesday shift is anchored on 27 October 2026, 09:45–13:45
Melbourne. Its first permissible Rye start is 15:00, subject to normal booking
notice, location, travel and other busy events. Confirmed shift openings
replace that day's static evening opening; unknown days retain the fallback.

`calendar-treatment-sync.timer` reads today and tomorrow's PHS staff timetable
hourly, using the existing encrypted staff login in a temporary private
session directory. `calendar.phs.write_pair` selects the dedicated PHS calendar,
separately from the primary `calendar.write_pair`. Five previously created
treatment entries were moved to `davidj4phs@gmail.com`, each destination verified
before its personal-calendar copy was removed. It writes only David's treatment times. It never fetches
consultation forms, and only cancels its own future events after complete
source reads. These intervals do not establish the designated shift finish.

Employment Hero roster access is verified using the phone-exported browser
session. The first cookie import stopped authenticating; a fresh export after
David completed login passed roster reads across three separate browser
launches. Refreshed cookies are encrypted and saved atomically after verified
reads. Seven assigned shifts were synced into the dedicated PHS calendar for
22 October–5 November. No shift hours are inferred from treatment times.
Today and tomorrow's shifts become free work-hours context only after a
successful treatment sync; their actual appointment intervals remain busy.

David shared `davidj4marlospa@gmail.com` with the primary account. CalDAV read
and write privileges are verified. It is now synced as `cal_davidj4marlospa`,
and `calendar.marlo.write_pair` records that destination. The confirmed
fortnightly shift was copied there and verified before removing its managed
primary-calendar copy. Both 27 October and 10 November occurrences read back
from the Marlo calendar, and Rye's availability refresh succeeded.

For interactive Employment Hero login, `agent-config/bin/employmenthero-browser-login`
opens a dedicated visible Chrome window and saves its session only after
the protected employee page loads. Only SOPS-encrypted dotenv reaches disk;
private errors and session values are never printed. `--desktop` waits up to
twenty minutes and saves automatically. On hpo (WSL), it explicitly uses WSLg's
Wayland socket. Launch it from Windows PowerShell if SSH-launched windows are
not visible. A one-off `employmenthero-login-watch` service copies only the
encrypted file back to red5, checks it can be decrypted privately, and reports
either completion or timeout via the alert store. Stored browser state is not
yet proof that the server can retrieve published rosters. The WSL runtime was
attached to Windows Session 0, so visible Linux Chrome did not appear on the
user's desktop. Those dedicated helpers were stopped after David chose phone
Firefox instead. `employmenthero-cookie-import` reads a site-only JSON or
cookies.txt export from phone Downloads, filters to Employment Hero's payroll
domains and writes a SOPS-encrypted `browser-pending.env` directly from memory.
It preserves any verified `browser.env` and the phone export until server
verification succeeds. Never paste cookie values into chat. Six synthetic
tests cover formats, scope, redaction, paths and ciphertext-only storage.

The `employmenthero-roster` reader emits only assigned shift IDs and
StartTime/EndTime from the employee schedule endpoint. The API's `allDay` flag
is a display convention; `Biddable` shifts are excluded. `media calendar
roster-sync` has regression coverage for local time, cancellation ownership and
failed sources. `calendar.phs.roster_command` and `calendar.phs.node` configure
the reader and a stable absolute Node path. The hourly treatment job now runs
roster-sync, which includes treatment refinement; if the roster login fails it
still attempts the independent treatment sync and reports failure. Subsequent
login expiry can still require David to provide a new session. No MFA codes
or authenticator setup secrets are retained. Rye uses the real designated end
plus 75 minutes, including when the shift context is marked free.
