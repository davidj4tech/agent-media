# Proposal: the phone as an agent's eyes and hands (1 Oct 2026)

Status: **proposed 1 Oct 2026, nothing built** (David picked it from a list
of new directions). Adds a phone-ask store and routes to the server (a new
§6.21 in `server-contract.md`), one frame to `GET /sessions/events` (§6.13),
an MCP tool and a `media phone` command in core, and an ask notification,
an Ask screen and the capabilities themselves to Sasonica's Android shell.

## What prompted it

An agent working on the house often needs one fact only the phone has: what
the router's lights look like, where David is, whether the phone should be
quiet for the next hour. Today it asks in words and David does it by hand.
The idea: the agent asks the phone directly — "take a photo: show me the
router lights" — David says yes on the phone, and the answer comes back to
the agent's tool call. Every request asked, nothing standing.

## What exists

- **An ask the phone answers — the mic asks** (§6.20). `mic.py` keeps open
  asks in memory with a TTL (`mic.py:33-38`), one per asking device
  (`mic.py:92-95`), wakes the stream at once (`mic.py:54-58`,
  `session_events.py:188`), and the stream carries a `mic` frame only to a
  client that asked for it (`app.py:736-742`). The phone diffs by id: a new
  one is a notification, a gone one is taken down
  (`NotifyService.java:563-585`, posted at `:587-622`, 90 s timeout at
  `:110`). This is the shape to copy.
- **Answering from a notification, app closed.** `AnswerReceiver.java:20-31`
  POSTs with `NotifyService.creds()` (`NotifyService.java:189`); the
  answer-from-the-notification proposal puts Allow behind the unlock
  (`setAuthenticationRequired`).
- **Files from the phone.** `POST /upload` (§6.18, `uploads.py:76`) keeps a
  file under `~/shared/<date>/` and returns its path; `GET /upload?path=`
  gives it back for the thread's chips. `ShareInPlugin.java:148` already
  streams a file to it natively.
- **Do Not Disturb.** The manifest already declares
  `ACCESS_NOTIFICATION_POLICY` (`AndroidManifest.xml:179-182`) and the app
  reads the filter when granted (`speech/Holds.java:149-150`,
  `speech/RingerState.java:22`). The same grant lets it *set* the filter.
- **A camera without the camera permission.** The app declares no `CAMERA`
  (`AndroidManifest.xml:166-184`); the attach button already offers the
  camera through the WebView's file chooser (`app/components/AttachButton.tsx:3`).
  A `FileProvider` is set up (`AndroidManifest.xml:86-94`, cache path in
  `res/xml/file_paths.xml`).
- **A tool that blocks for a human.** `converse` in
  `mcp_server.py:240-317` speaks, waits up to `timeout_s`, and returns
  `{"reply"}` or `{"reply": None, "reason": "timeout"}`. The narrow
  entrypoint picks agent-initiated tools (`mcp_server.py:1401-1408`).
- **A host caller's token.** `POST /alerts` admits the host's own amux
  token (`app.py:1439-1440`; the check is `canvas.py:426-437`, the token
  `~/.amux/auth_token`). A core tool on red5 can call the server the same way.
- **Which session asked.** The PreToolUse hook is handed every tool call
  with its session id; `pending_asks.py:1-15` already keeps one file per
  session from it.
- **Not there:** no location, camera, clipboard or calendar code, and no
  permission for any of them.

## The shape

[[visual: left to right: a box "agent (Claude, red5)" calling "phone_ask(photo, 'show me the router lights')" → a box "server: phone ask store (id, kind, why, thread, 5 min)" → an arrow labelled "/sessions/events phone frame" → a phone with a notification "Photo for agent-media · show me the router lights" and buttons "Allow…", "Deny"; from Allow an arrow to "unlock → camera" → "POST /upload" → "POST /phone/answer {id, path}" → back to the agent box labelled "returns {status: ok, path}"; a side box under the server "audit: phone-asks.jsonl"; dotted branches from the phone: "Deny → denied", "no answer → timeout", "no phone connected → no_phone"]]

### 1. The ask store (server)

A new `phone.py`, modelled on `mic.py`. An ask:
`{id, kind, why, session, title, at, expires, status}`. `kind` is one of
`photo`, `location`, `dnd`, later `clipboard`. `why` is the agent's one
sentence, shown as-is (capped at 200 characters). Kept in memory; a restart
drops open asks, and the waiting tool sees `gone`.

- `POST /phone/ask {kind, why, session?, params?}` — host token only (an
  agent on the host), not a device. Returns `{id}` at once. One open ask
  per session: a second one replaces the first.
- `GET /phone/ask?id=&wait=<s>` — long-polls until the ask is answered or
  the wait ends; the tool loops on it.
- `POST /phone/answer {id, decision, result?}` — a paired device. `decision`
  is `allow` or `deny`; `result` is the capability's answer (below).
- `POST /phone/cancel {id}` — the tool gave up, or the agent was stopped.
- `phone` frame on `/sessions/events?phone=<caps>`: every open ask whose
  kind is in the connecting device's `caps` (`photo,location,dnd`). Sent like
  `mic`: after the first frame if any, then on each change.

If no stream with `?phone=` is connected when the ask arrives, the answer
is `no_phone` at once, not a five-minute wait.

### 2. The tool (core)

`phone_ask(kind, why, timeout_s)` in `mcp_server.py`, in the narrow set,
and `media phone photo|location|dnd "<why>"` in the CLI for harnesses
without MCP (Codex, pi). Both block like `converse` and return one of:

- `{status: "ok", result}` — photo: `{path, width, height}` (a file under
  `~/shared/`, which the agent opens with its own Read tool); location:
  `{lat, lon, accuracy_m, at}`; dnd: `{on: true, until}`.
- `{status: "denied"}`, `"timeout"`, `"no_phone"`, `"gone"`.

The tool's docstring says the plain rule: ask only for what the task needs,
say why in one sentence, and on `denied` do not ask again for the same thing.

### 3. The phone (app)

`NotifyService` handles the `phone` frame like `mic`: a new id posts a
heads-up notification on a new channel, "Agent asks", titled
"<Photo / Location / Quiet> for <thread title>", with `why` as the text.
Two buttons: **Allow…** and **Deny**.

- **Deny** is a broadcast to an `AskReceiver` (like `AnswerReceiver`), no
  unlock: `POST /phone/answer {id, decision: deny}`.
- **Allow…** is an *activity* intent, so it always asks for the unlock and
  then opens a small Ask screen with the app in front. Every capability then
  runs as a foreground app, which avoids the background limits on camera,
  location and clipboard. The Ask screen repeats the request and does it.
- The app open on that thread also shows the ask as a card at the top, with
  the same two buttons.
- The lock screen's public version says only "An agent is asking for
  something" — no `why`, no thread.

### 4. The capabilities, and what each needs

| Kind | How | Android permission |
|---|---|---|
| `photo` | `ACTION_IMAGE_CAPTURE` into a `FileProvider` cache file; David frames and shoots; downscaled to 1600 px, uploaded with `/upload`, then answered | none, as long as the app never declares `CAMERA` |
| `location` | one `getCurrentLocation` fix while the Ask screen is up | `ACCESS_COARSE_LOCATION` (+ `FINE` if David wants it), asked the first time; never background location |
| `dnd` | `setInterruptionFilter(PRIORITY)`; the app keeps the old filter and sets an alarm for `until` to restore it, only if nobody changed it since | the existing Do Not Disturb access grant; no new permission |
| `clipboard` (later) | read once on the Ask screen (Android lets only the focused app read it, and shows its own toast) | none |
| calendar | **not proposed**: the agent already reads David's calendar through the Google Calendar connector, so `dnd` takes `until` from there | — |

### 5. Privacy

- **Every request is asked.** No "always allow", no standing grant, no
  silent capture. An ask without a tap does nothing.
- **Allow needs the unlock**; Deny does not.
- **Nothing kept on the phone.** The photo's cache file is deleted after the
  upload; the location is not stored by the app.
- **An audit list.** Every ask is appended to
  `<state_dir>/phone-asks.jsonl`: when, which thread, kind, why, outcome
  (allowed / denied / timeout / no_phone), and for a photo its path — the
  coordinates themselves are not logged. `GET /phone/asks` (gated) serves
  the last 100, and Settings → **What agents asked** shows them.
- A **switch per capability** in Settings (all on): off means the app does
  not offer it in `?phone=`, so the agent gets `no_phone` for that kind.

### 6. Unreachable, or no answer

- No phone stream with that capability: `no_phone` at once.
- Phone connected but no answer: the ask expires (photo 5 min, the others
  2 min — David may have to walk to the router), the notification is taken
  down (`setTimeoutAfter`, and the frame without it), the tool returns
  `timeout`.
- Allowed but the upload or the fix fails: the app answers
  `{decision: allow, error}` and the tool returns `{status: "failed", error}`.
- Two phones: whichever answers first wins; the ask leaves both frames.

## Order

1. **Server + tool, photo only.** `phone.py`, the routes, the frame, the
   audit file, `phone_ask` and `media phone photo`; tests in
   `test_phone.py` and `test_session_events.py`; contract §6.21.
2. **App, photo.** The frame, the notification, `AskReceiver`, the Ask
   screen with the camera, the upload, the answer; JUnit for the frame diff.
3. **dnd** (no new permission), then **location** (the first new runtime
   permission). Settings switches and **What agents asked**.
4. Clipboard only if a real need shows up.

## Open questions

1. **Photo first?** Recommended: it is the one David named, needs no new
   permission, and has the clearest value.
2. **Location precision**: coarse only (a few hundred metres, enough for
   "is David home"), or fine as well?
3. **Which phones**: every phone with the capability (recommended, first
   answer wins), or only one chosen device?
4. **Long waits**: does Claude Code's MCP call allow a five-minute block,
   or should the tool return `pending` with an id and a second
   `phone_result(id)` call? Check the MCP client timeout before step 1.
5. **Which thread asked**: take the session from the PreToolUse hook (as
   `pending_asks.py` does), or let the tool send its cwd and the server
   match it? The hook is more reliable; the cwd works without hooks.
6. **Sasonica Shell's assistants** (claude.ai via the connector): should
   they get `phone_ask` too, through the shell's typed tools? Not in this
   proposal; the same store would serve them.

## Verification

- `pytest packages/server/tests/test_phone.py test_session_events.py`:
  ask, frame, answer, deny, expiry, `no_phone`, replace-per-session, the
  audit line.
- With the phone stream stopped: `media phone photo "test"` returns
  `no_phone` within a second.
- On p8a, app closed, phone locked: `media phone photo "show me the desk"`
  → the notification; Deny → `denied`, no unlock asked. Again, Allow → unlock
  → camera → the tool returns a path under `~/shared/`, the file opens, the
  audit list shows both.
- Ignore one: after 5 min the notification is gone and the tool says
  `timeout`.
- `dnd` with `until` two minutes away: the phone goes quiet, then comes
  back to what it was; set it by hand in between and it is left alone.
