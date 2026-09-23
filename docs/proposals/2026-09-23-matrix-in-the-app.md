# Matrix in the app — the homeserver, and its gateways, as threads

2026-09-23. David's ask: Sasonica should reach the Matrix server and its
gateways from inside the app. Matrix is already on red5 and agent-media
already listens to one room; what is missing is that the app cannot see any
of it, and there are no gateways running at all. This is what is there, the
one fork in the road, and the cheapest order to build it.

## What the investigation found

**1. The homeserver is live, public and federating.** `tuwunel 1.6.0`
(`tuwunel.service`, user `tuwunel`), config `/etc/tuwunel/tuwunel.toml`:
`server_name = "ryer.org"`, registration closed, nightly backups
(`matrix-backup.timer`, backup #30 as of this morning). It listens on
`127.0.0.1:8008` only; Caddy publishes it — the apex `ryer.org` serves
nothing but the two delegation documents (`m.server` →
`mel.ryer.org:443`, `m.homeserver` → `https://mel.ryer.org`) and
`mel.ryer.org` reverse-proxies `/_matrix/*` to 8008.
`/_matrix/client/versions` answers up to `v1.15`.

**2. There is exactly one room, and it is not encrypted.** The token
agent-media holds is `@sam:ryer.org` (device `hZkfK2qfHu`), joined to a
single room `!OpAfreoGOnC7dOC4rA:ryer.org`, named **sam**: join rule
`invite`, history `shared`, no `m.room.encryption` state event at all,
members `@david`, `@mel`, `@sam`. So nothing built today has ever had to
deal with E2EE — and nothing built today *could*.

**3. agent-media already has a Matrix intake, and it is one-way.**
`agent-media-intake-matrix.service` runs `media-intake-matrix`
(`packages/intake-matrix`, 321 lines, **standard library only** for the
Matrix protocol). It long-polls `/sync` on one allow-listed room, downloads
voice messages and audio attachments and plays them through `SinkSpeech`,
and maps `!pause` / `!resume` / `!skip` / `!replay` onto the coordinator.
Its docstring is explicit about the boundary: *"Recording / sending back to
the room is dropped here."* Config is `MATRIX_HOMESERVER`,
`MATRIX_ACCESS_TOKEN`, `MATRIX_SAM_ID`, `MATRIX_CONTROL_IDS`,
`MATRIX_ROOM_ALLOW` — the per-room allow list already exists.

**4. There are no gateways.** No `/opt/mautrix-*`, no
`/etc/matrix-bridges/`, no bridge process, no appservice registration on
this host. The mautrix-meta (Messenger/Instagram) and mautrix-gmessages
(SMS/RCS) install written up in `agent-memory/docs/MATRIX_BRIDGES.md` — with
its `@smsbot:ryer.org` pairing runbook — describes the **Synapse** era on
the pi5, whose drive died in August; `~/org/inbox.org` has that doc and its
compose files queued for deletion. So "the gateways" are a thing to build,
not a thing to connect to.

**5. tuwunel takes appservices, so the gateways do not need Synapse back.**
The binary carries the admin commands (`Register an appservice using its
registration YAML`, `Appservice registered with ID:`, `appservice_dir`,
`appservice_timeout`, `Appservice tokens must be used on this endpoint`).
A mautrix bridge attaches the same way it would to Synapse: a registration
YAML, registered once in the admin room.

**6. The app is already the right shape for a room.** The chat app reads
`/targets` for the list and the §11 stream plus §6.2.2 messages for one
conversation, with a composer that posts §6.3. A Matrix room is the same
three verbs the 21 Sep source-adapter proposal named — **list, capture,
send** — and the tool-step, ask and stop machinery simply never fires for a
human conversation. Two things it would gain for free are already on the
books as gaps: `server-contract.md` §16 lists push notifications as "none.
Matrix or FCM; out of scope here", and `app-redesign-options.md` already
flags Matrix as a transport that "would give push, multi-device and threads
for free — and there is already a Matrix server on red5".

## The fork

**A. A server-side source adapter.** The canvas speaks Matrix; rooms appear
on `/targets` as threads; the app is unchanged.

**B. A Matrix client in the app.** `matrix-js-sdk` plus the rust-crypto
WASM in the Capacitor WebView: real E2EE, its own sync, its own IndexedDB
store, its own push registration.

**A is the recommendation**, and not narrowly: B duplicates the thread
list, the timeline, the offline cache, the draft handling, the speech bar's
notion of "a reply arrived" and the pairing model — a second app inside the
app, whose only advantage over A is end-to-end encryption. And E2EE is a
decision that can be made once, on the server, for both (§"Encryption"
below). Start with A; B stays the answer only if the rooms that matter must
stay unreadable to red5, which is the opposite of what the canvas is for.

## The proposal

A `matrix` source next to `tmux` and `herdr`, with the same three verbs:

| | list | capture | send |
|---|---|---|---|
| tmux | `list-panes` + `/proc` | `capture-pane` | `send-keys` |
| herdr | `herdr agent list` | `herdr pane read` | `herdr pane send-text` |
| **matrix** | `/sync` joined rooms | `/rooms/{id}/messages` | `PUT /rooms/{id}/send/m.room.message/{txn}` |

Concretely:

- **One sync loop on the server, not two.** The intake daemon's `/sync`
  moves into (or behind) the canvas, and the speech intake becomes a
  consumer of it. Two long-polls with the same access token on the same
  device is how you lose events; this is step 0 and it changes nothing the
  user sees.
- **Rooms on `/targets`.** A row per allow-listed room: `title` = the room
  name, `recap` = the last message as the preview line, `project: null`,
  and a new `source: "matrix"` (the §6.16 rows already carry a `source`, so
  the key exists). Never live, never rested, never pinned — none of the
  session machinery applies.
- **The timeline is the capture.** `/messages` with `from`/`dir=b` maps
  straight onto §6.2's `before` paging; `m.room.message` bodies become text
  parts, the sender becomes the role (`@david` → user, everyone else →
  assistant, with the display name shown because a room can have more than
  two people). Images and voice notes become the attachments the contract
  does not have yet — first cut can show "sent a photo" and a tap-through.
- **Sending is the composer, unchanged.** `PUT …/send/m.room.message/{txn}`,
  the txn id doubling as the echo dedupe when the event comes back on sync.
- **Who the app speaks as.** The only token here is `@sam`'s. If the app
  sends with it, everything David types on his phone arrives in the room as
  the agent. The app should send as **`@david`, with its own device** —
  one more access token in `secrets.env`, and the room is then read by
  `@sam` and written by `@david`, which is also what makes a bridged SMS go
  out from the right person.
- **Where it shows.** Its own filter value on the threads screen
  ("Messages"), not mixed into the coding rows: the Smart sort's
  needs-you/working states mean nothing for a room, and a room's unread
  count is a better signal than any of them.

### Encryption — the decision to make once

The stdlib intake cannot read an encrypted room, so today's answer is
"the rooms the canvas serves are unencrypted", which is already true of
`sam`. That holds fine for agent rooms. It does **not** hold comfortably
for gateways: portal rooms carrying real SMS and Messenger threads. Two
honest choices:

1. **Leave the served rooms unencrypted** (mautrix bridges default to
   encryption off; it is a per-bridge setting). Cheapest, and the messages
   are in plaintext on red5 either way once the canvas can read them.
2. **Give the server a real client** — `matrix-nio[e2e]` (libolm) or the
   rust SDK through bindings — with a verified device of its own. This is
   the honest version, and it is a real dependency: the intake's "standard
   library only" property is gone the day it lands.

Either way the server sees the plaintext. The security boundary that
actually matters is not E2EE, it is **which rooms** — keep
`MATRIX_ROOM_ALLOW` as the gate, opt-in per room, default nothing.

### The gateways

Once rooms are threads, a gateway is a server-side install and **no app
change at all**: a mautrix bridge registers with tuwunel, its management
room and its portal rooms appear as more rooms, and pairing (the QR / login
flow) happens by typing `login` into the management room *from the phone*.
Candidates, in the order they earn their keep: **gmessages** (SMS/RCS —
the one that removes a reason to pick up the phone), **whatsapp**,
**signal**, **meta** (Messenger/Instagram). Each is its own process, its own
config, its own state directory and its own re-pairing when the upstream
account logs it out — that recurring cost is the real price of a gateway,
not the install.

### Two things that must not happen by accident

- **A bridged message speaking out loud.** The intake's whole purpose is to
  play what arrives through `SinkSpeech`. The day SMS lands in a room, that
  path will read texts aloud in the house. Speech must be opt-in per room,
  and off by default for anything bridged.
- **Every personal thread into the transcript cache.** The canvas caches
  and summarises what it reads. A gateway turns that into an archive of
  David's messaging. Allow-list per room, and keep the app's device tokens
  (§9) as the only way in.

## Order of work

0. **One sync loop**, server-side; the intake keeps working (no user-visible
   change).
1. **Read-only**: Matrix rooms on `/targets`, timelines through §6.2.2,
   updates on the §11 stream. The app gets a "Messages" filter and nothing
   else new.
2. **Send**, as `@david` with its own token.
3. **One gateway** — gmessages — registered on tuwunel. No app change; the
   proof is that pairing works from the phone.
4. **Unread and push**: room unread counts into `/dashboard`'s "Needs you";
   real push only if a push gateway (sygnal + FCM) is worth it, which is the
   same question §16 already parks.

Steps 0–2 are the proposal proper. Step 3 is the part that answers "and its
gateways", and it is deliberately last of the build steps, because until a
room is a thread a bridge has nowhere to show.
