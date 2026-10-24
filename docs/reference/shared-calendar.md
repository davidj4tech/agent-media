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

A booking site's own mirror lives outside this repo (David's Rye Remedial
mirror and its client texts moved to agent-config, `rye-bookings`, on 25
October 2026). It uses only generic pieces from here: `media calendar
put`/`delete`/`get` (`get --uid UID` answers the event's ETag, start and end;
`--no-sync` batches writes), the alert store with action buttons that run a
configured command (`[alerts.actions]`, below), `media sms send` (below) and
the availability publication's `before` hook.

### Texts: `media sms send`

`media sms send --to +61… [--txn ID]`, the text on stdin, sends one SMS from
the owner's own number through the Google Messages bridge (`sms.py`,
mautrix-gmessages, the bridge the Messages tab uses). The number must be in
international form; it is used for that one send and never logged, stored or
printed, and errors (exit 2, on stderr) never carry it. `--txn` is the Matrix
transaction id, so a retry after an unclear failure is the same message. It
calls the bridge's provisioning API `POST
/_matrix/provision/v3/create_dm/<+61…>?user_id=<owner>` (bearer = its
`provisioning.shared_secret`, found as `bridges.py` finds it) for the
number's portal room, joins it as the owner and sends `m.text` with
`MATRIX_OWNER_TOKEN`. Config `[sms]`: `bridge` (default `gmessages`),
`login_id`, `matrix_env` (default `~/.config/agent-media/matrix.env`, read
when the process environment has no Matrix login).

### Alert buttons that run a command

An alert's `actions` (server-contract §6.17) run in-process handlers
(`alerts.ACTION_HANDLERS`) or, for a producer outside this repo, a command
named in config:

    [alerts.actions]
    "calendar.rye.booking." = ["~/.local/bin/rye-bookings", "alert-action"]

The canvas runs `argv… <alert_id> <action>` (up to 150 s) and takes its last
stdout line, `{"ok": true|false, …}`, as the answer; a non-zero exit or no
JSON is a refusal (502). Restart the canvas after changing the table.

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

`calendar-availability-sync.timer` publishes every ten minutes to a booking
site (Rye Remedial) over authenticated public HTTPS. Its owner-only
configuration is `~/.config/agent-media/calendar-publish.json`: `endpoint`,
`token` (the matching scoped token lives in Drupal State and is never
exported to Git), and optionally `zone` (default the host's), `days` (default
40), `shift_sources` (`{"<managed source>": <minutes>}`: a shift from that
source opens the day that long after it ends; here `{"marlo": 75,
"phs-shift": 75}`), `own_uids` (a regex for the site's own bookings mirrored
into the calendar, left out so they are not echoed back as outside conflicts;
here `rye-booking-\d+@ryeremedial\.com\.au`) and `before` (an argv run
first; here `rye-bookings sync --no-local-sync`; its failure stops the
publication). The payload contains only busy intervals and `open_after`
times, not event titles, locations, client identities or source URLs. Every publication requires a successful
vdirsyncer run. New bookings close if the last verified snapshot exceeds two
hours; existing bookings stay in place, and conflicts raise an alert.

Marlo's fortnightly Tuesday shift is anchored on 27 October 2026, 09:45–13:45
Melbourne. Its first permissible Rye start is 15:00, subject to normal booking
notice, location, travel and other busy events. Confirmed shift openings
replace that day's static evening opening; unknown days retain the fallback.

### Managed sources: `media calendar reconcile`

Readers this repo should not know about (a roster, a staff timetable, any
feed) keep their own events in a calendar with `media calendar reconcile
--source NAME --calendar PAIR --start D --end D`, the complete list for that
range on stdin as `{"events": [...]}` (or a bare list; `--event FILE` reads a
file). Each event is a `put` event (`uid`, `title`, `start`, `end`, optional
`description`, `location`, `busy`). New ones are created, changed ones updated
under their ETag, finished ones left as they were. Entries tagged with the same
source, inside the range and not yet started, that the list no longer has are
removed; nothing else can be. `--refuse-empty` fails, removing nothing, on an
empty list when entries exist; `--no-sync` skips the closing vdirsyncer sync.
A reader that fails must not call it. Source names are lowercase letters,
digits and dashes (`calendar_write` tags them `X-SASONICA-SOURCE`).

David's Peninsula Hot Springs shifts (`phs-shift`) and treatments
(`phs-treatment`) are such a reader: `agent-config/bin/phs-calendar-sync` on
`phs-calendar-sync.timer`, hourly (moved out of agent-media 25 October 2026,
replacing `calendar-treatment-sync.timer` and `media calendar roster-sync` /
`treatments-sync`). Its notes live with it. Today and tomorrow's shifts become
free work-hours context only after a successful treatment read; the
treatments then carry the busy time.

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

Rye uses the real designated end plus 75 minutes, including when the shift
context is marked free.
