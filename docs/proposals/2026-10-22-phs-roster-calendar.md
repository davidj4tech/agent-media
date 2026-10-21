# PHS calendar: placeholders, published shifts and treatment appointments

David, 22 October 2026: keep a visual placeholder for weeks at PHS, replace
guesswork with published working hours about two weeks ahead, then use
actual treatment times the day before each working day.

## Current state

The primary Google calendar contains a fortnightly Wednesday “Spa Therapy”
series. Its original duration is six days and eight hours, so it works as a
week marker but was incorrectly marked busy throughout that week. The series
has now been marked **free**, preserving its dates, recurrence and colour.
Google read-back verified that change. Speech attention and meeting rules
remain unchanged; actual busy events should still hold speech normally.

Employment Hero's employee portal supplies the published working hours.
The public login is email-first; authenticated access and the roster data
shape are not yet verified. Employment Hero documents a calendar subscription
for published shifts, but availability depends on the organisation/product:
[official calendar-sync instructions](https://help.employmenthero.com/hc/en-au/articles/17426887868303-Calendar-sync-How-to-sync-your-calendars-with-rosters).
Prefer that feed if this account exposes it; do not assume the HR feature
also exists in the Payroll Classic employee portal.

The existing dotfiles `phs-session` already authenticates to Netbookings
using SOPS. `phs-rundown` demonstrates reading a chosen date's timetable.
Its client names and consultation-form data are unnecessary here: the
calendar reader should fetch only the staff grid, retain David's own time
windows and opaque source IDs, and never fetch consultation forms or copy
client details into calendar descriptions.

## Private Employment Hero setup

`deploy/employmenthero-cred-setup` prompts in a terminal with hidden input.
It pipes JSON directly to SOPS using the existing dotfiles-secrets policy,
then atomically saves only encrypted dotenv output to
`~/dotfiles-secrets/secrets/employmenthero/employee.env`. The file is 0600
and its directory 0700. Neither the password nor email enters command-line
arguments, shell history, environment variables or a plaintext temporary
file. Nothing is committed by the helper.

The script collects credentials only; it does not claim to authenticate.
After David runs it, verify login with credentials passed directly from
SOPS to the browser process. Request MFA interactively if required; do not
store MFA codes. Prefer a private reusable browser session or a roster feed
over repeated password logins. Never print decrypted values, cookies, feed
tokens, page captures or request bodies into a chat or log.

## Sync behaviour to implement after access is verified

1. Retain the **free** week marker so PHS weeks remain visible at a glance.
2. Check for published shifts in a rolling 14-day window. Use confirmed
   shift times for busy working-hours entries; leave unresolved days alone.
   A missing or unpublished roster is not evidence of a day off.
3. The evening before a confirmed working day, retrieve its Netbookings
   appointment windows. A successful, complete reading refines the busy
   work block into actual busy treatment intervals; keep the working-hours
   context as free if useful. Refresh on the working morning and update
   changed appointments. Preserve real gaps rather than treating the entire
   PHS week as continuous work.
4. Give every managed event a stable source identity (shift/booking ID and
   occurrence date). Repeated runs update the same event, without duplicates.
   A cancellation removes only an event owned by this integration, and only
   after a fresh, complete source result confirms it.
5. Use Australia/Melbourne with explicit offsets, including daylight saving
   and overnight shifts. Never rewrite past calendar history or the whole
   placeholder series because one day becomes known.
6. On login expiry, missing permissions, unavailable sources or parsing
   errors, keep the last verified calendar entries and report a failed sync.
   Do not convert a failure or ambiguous empty page into destructive changes.

Shared CalDAV writing and the PHS treatment sync are now installed. The
published-roster sync still depends on an interactive Employment Hero login
or verified feed. No PHS designated shift hours have been guessed. The
time-only Netbookings reader independently syncs today and tomorrow hourly.

## Marlo's Spa and Rye Remedial availability

David also asked for a fortnightly Tuesday **09:45–13:45** Marlo's Spa
shift in the calendar, and for Rye Remedial treatments to become available
**one hour after the designated shift ends**. Use Australia/Melbourne.
David confirmed the first Tuesday as **27 October 2026**. The recurring
event was created and verified through the existing CalDAV connection,
independently of OpenAI's connector. Existing `marlo-session` / `marlo-bookings` tools
already read Timely, should confirmed Marlo's treatment times be needed.

Calendar reads now have a shared local interface: `media calendar` and the
stdio MCP `calendar_events` tool. They read the existing vdirsyncer copies
without OpenAI's connector; see [shared calendar access](../reference/shared-calendar.md).
Writes are available through `media calendar put` / `calendar_write`, with
stable UIDs, ownership checks and conditional ETag updates/deletes.

Preserve a shift's real end time in Google Calendar. Apply the one-hour
offset in Rye Remedial's availability calculation: a Marlo's shift ending
13:45 permits Rye Remedial starts from **14:45**. Across several shifts
on one day, use the latest designated shift's end, then add one hour. A
booking is still subject to its normal notice, latest-start, travel and
existing-appointment constraints. Never move an existing customer booking
automatically when a shift changes; surface the conflict for the owner.

The inspected website currently uses fixed fortnightly opening times in
`rye_booking/src/Availability.php`, configured by `rye_booking.settings`.
It now consumes timing-only calendar snapshots over scoped HTTPS. Confirmed
calendar shift hours replace the fixed opening time for their day,
rather than merely layering a later static evening opening over an earlier
shift finish. Unknown roster days retain the existing fallback; do not
infer that they are available all day. Free week markers are not work shifts
and must not create closures. Other real busy events still prevent overlaps.

The website integration is deployed. Snapshots refresh every ten minutes,
contain only busy intervals and opening times, and never contain titles,
client data or source URLs. A successful vdirsyncer run is required before
publication. After two hours without a fresh snapshot the site closes new
slots; existing bookings stay in place and conflicts raise an alert. The
live booking service verified the 13:45 → 14:45 Marlo boundary, with zero
existing booking conflicts. Both installed timer jobs completed successfully.
Employment Hero's email-first browser flow remains at the email screen;
David confirmed interactive login is needed. A reusable authenticated browser
session or the account's private roster subscription is the remaining input.

## Validation

The credential helper has synthetic tests for hidden intake, punctuation
and Unicode, JSON-over-stdin encryption, redacted failures, private file
permissions, atomic replacement and symlink rejection. A real SOPS smoke
test used synthetic credentials only and made no website login attempt.

Before enabling sync, test unpublished and empty rosters, refreshed and
cancelled shifts, overnight/DST boundaries, per-day treatment refinement,
idempotency, credential expiry and source failures using fixtures. Check
that the original week marker remains free and that no client identity or
health information enters a calendar event or log.
