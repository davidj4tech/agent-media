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

The regular sync and calendar writer are **not yet installed or enabled**.
They depend on authenticating to Employment Hero, verifying the published
roster/feed and its coverage, and checking the first proposed calendar diff.
No fabricated shifts or guessed treatment slots have been written.

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
