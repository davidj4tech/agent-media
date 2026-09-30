# Proposal: answer a question from its notification (1 Oct 2026)

Status: **steps 1–2 and the lock screen's unlock-on-Allow built 1 Oct 2026**;
the Settings switch is still to come (David picked it from a list of new
directions: "Approve from notification"). Adds a trimmed `approval` to the
`sessions` frame of `GET /sessions/events` (§6.13), and action buttons plus a
broadcast receiver to Sasonica's `NotifyService`. No new route.

## What prompted it

"Needs you · <title>" today is only a doorway: tap, unlock, the app opens,
the thread loads, then the answer. Most questions are one tap — Allow /
Deny, or one of two or three options — and could be answered where they
arrive, including with the phone pocketed and only the lock screen at hand.

## What exists

- `NotifyService.java` holds `/sessions/events` and posts one notification
  per thread (`act()`, id `0x5a5102`, channel `needs-you`); `NotifyRules`
  posts NEEDS_YOU on the move *into* `approval` and cancels on the move out.
  Its only action button is the status notification's "Turn off".
- The frame's rows are `{session, title, state}` — no question
  (`session_events.rows_of()`).
- `POST /session/answer` (§6.4) already takes `{session, choice, key}` for a
  permission prompt or one single-select question, and a headless session's
  `{session, request_id, decision}`. A stale `key` gets 409 with the current
  `approval`, so a late tap cannot answer the wrong question.
- `NotifyService.creds()` gives native code the base and device token, and
  `VoiceAlerts` already POSTs with it — no WebView needed.
- `dashboard.py`'s `_approval()` already builds the full approval for a row
  in `approval` state.

## The shape

[[visual: phone lock screen with a notification "Needs you · agent-media — Run the tests?" and three buttons "Yes", "No", "Open"; an arrow from "Yes" to a box "AnswerReceiver (no app)" then to "POST /session/answer {choice, key}"; branches: "200 → Answered ✓ then gone", "409 → re-post the new question", "error → Couldn't send · tap to open"]]

### 1. The frame carries the question (server)

For a row whose `state` is `approval`, `rows_of()` adds a trimmed copy:
`approval: {key, id?, kind, question, options: [{n, label}], multiSelect,
partial, several}` (`several` = more than one question; `free_text` dropped
— a free-text answer opens the app). Built with `_approval()` from
`dashboard.py`, only for waiting rows (rarely more than one or two), on the
existing 3 s sweep. A change of `key` counts as a change, so a follow-up
dialog sends a new frame. Contract §6.13 and `tests/test_session_events.py`.

### 2. Buttons on the notification (app)

`NotifyRules` also posts NEEDS_YOU when the `key` changes while the row
stays in `approval` (the next dialog replaces the last). `act()` adds up to
three actions when the question is answerable in one tap:

- **Tool permission:** Allow, Deny, Open.
- **One single-select question, ≤ 2 options:** each option, then Open.
  Three or more options: the first two and Open.
- **Anything else** (multi-select, several questions, `partial`, free text):
  no answer buttons — the tap opens the thread, as today.

Each button is a `PendingIntent.getBroadcast` (IMMUTABLE) to a new
non-exported `AnswerReceiver` with `session`, `key`, and `choice` or
`request_id` + `decision`.

### 3. The receiver sends it

`AnswerReceiver` takes `goAsync()`, reads `NotifyService.creds()`, and POSTs
`/session/answer`:

- **200:** the notification becomes "Answered · <label>" (silent) and
  clears after a few seconds; if the response carries a next `approval`, the
  next frame posts it.
- **409:** re-post with the `approval` the 409 returned ("The question
  changed").
- **Anything else:** "Couldn't send · tap to open", the tap opens the thread.

### 4. The lock screen

Answering a question from the lock screen is the point; approving a *tool*
there means anyone holding the phone can let an agent run a command. So:
**Allow** on a tool permission is `setAuthenticationRequired(true)` (Android
12+: the unlock prompt comes first); Deny and question options are not.
The notification's public version on the lock screen shows the title and
question only, no tool input. A Settings → Notifications switch, **Answer
from the lock screen** (on), turns the unlock requirement on for everything.

## Order

1. Server: the trimmed `approval` in the frame, the key-change rule, tests,
   contract.
2. App: buttons + `AnswerReceiver` + the 200/409/error handling; JUnit for
   which buttons a given approval gets and for `NotifyRules`' key change.
3. Lock screen: authentication on Allow, the public version, the setting.
   A What's new line: "Answer a question straight from its notification."

## Verification

- `pytest packages/server/tests/test_session_events.py`.
- On p8a with the app closed: a headless session asks for a Bash permission
  → the notification has Allow / Deny / Open; Deny answers without opening
  the app and the thread shows it denied. Allow on the locked phone asks for
  the unlock first.
- An AskUserQuestion with two options → both as buttons; answer from the
  lock screen. One with multi-select → only the tap to open.
- Answer the same question at the desk, then tap the stale button → 409 →
  the notification shows the current state, nothing is sent twice.
