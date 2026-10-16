# Matrix as the app's transport (16 Oct 2026)

David, 9 Oct 2026: "both, rooms first". Rooms in the app are built: steps
0–3 of `2026-09-23-matrix-in-the-app.md` plus three bridges, b989ca1..98531e0,
8–11 Oct (`handover/2026-10-09-matrix.md`). The second half was never
written up: the app reaching its server *through* a homeserver, not over the
tailnet, a tunnel or the hosted relay. David, 16 Oct 2026: "let's get the
ball rolling on Matrix too". Status: **proposal, nothing built.**

## Where this sits

- **29 Sep, the split** (`2026-09-23-accounts-and-the-identity-seam.md`):
  Drupal on cms.sasonica.com owns accounts and is the OIDC issuer; a
  Sasonica homeserver on sasonica.com would own devices, rooms-as-threads
  and push. ryer.org stays personal (family, bridges).
- **4 Oct, roadmap 16:** the hosted relay was chosen over a sasonica.com
  homeserver as the next step towards other users. It is live at
  relay.sasonica.com and carries Sasonica Shell only.
- **8 Oct, roadmap 15:** the sasonica.com lookup (a Worker keeping each
  install's current quick-tunnel URL) went on hold pending Matrix. It is
  being revived today as the near-term fix for the address
  (`handover/2026-10-16-agent-media-permanent-server-address-off-the-tailnet.md`).
- **What the app exchanges today** (`server-contract.md`): request/response
  app routes (`app.dispatch`, which is exactly what the `--public` listener
  answers); two SSE streams, `/sessions/events` (sessions, alerts, mic, and
  the `speech`, `music`, `handoff`, `book` and `notes` frames) and
  `/threads/{id}/events` (§11); state reports back (`POST /speech/state`,
  `/music/state`, `/handoff/state`); `/upload` up to 512 MB and `/img/`;
  pairing by a desk-minted code redeemed at `POST /pair` (§9).

This proposal does not touch the personal rooms on ryer.org, which reach the
app through the canvas as threads. That path stays as it is.

## The recommendation, in one paragraph

Matrix becomes **an optional third way to reach a server**, beside a direct
address (tailnet, LAN, named tunnel) and the quick tunnel plus lookup. The
server and the phone are both **devices of one Matrix user**, and each dials
out to the homeserver, so neither needs an address. The HTTP contract stays
the only contract: the server dispatches what arrives over Matrix into the
same `app.dispatch` the public listener uses, so the exposed surface is
identical and no route is rewritten. The lookup is not throwaway: it stays
as the path for anyone with no account and no homeserver. Build it first
between red5 and p8a on ryer.org, with no new infrastructure, and measure it
before sasonica.com runs a homeserver for anyone.

## What moves, what stays

Two kinds of Matrix message do the work. **To-device events** are addressed
to one device, queued until delivered, then dropped, and never stored in
room history. They carry everything request-shaped or high-rate. **Room
events** are stored and synced to every member. They carry only what should
be a conversation that other clients can read: the threads-as-rooms step,
later.

| What | Today | Over Matrix |
| --- | --- | --- |
| App routes (`/targets`, `/conversation/log`, `/reply`, `/ask`, `/session/*`, `/org/*`, `/search`, …) | HTTP | a `com.sasonica.request` to-device event `{id, method, path, query, headers, body}` to the server's device; a `com.sasonica.response` `{id, status, headers, body}` back. The `Authorization` header rides inside, so `auth.gate` is unchanged |
| `/sessions/events`, every frame kind | SSE held by NotifyService | `com.sasonica.frame` to-device events, pushed by the same `poke()`; the app's `/sync` long-poll replaces the SSE. No `?ping=`, no 100 s Cloudflare cap |
| `/threads/{id}/events` (§11) | SSE while a thread is open | the same frames, while the app holds a subscription made by a request |
| `POST /speech/state` and the other state reports | HTTP, ~every 2 s while playing | requests like any other route, as to-device events: they never land in room history |
| `POST /pair` | HTTP, no auth | a request too; the code still proves the desk (see Pairing) |
| `/upload`, `GET /upload`, `/img/` | HTTP | HTTP when a direct path exists; otherwise the media repository, capped (see Uploads) |
| Desk routes (`/show`, `/ctl`, `GET /pair`, `/peek`, `/events`), canvas pages | local HTTP | **never** over Matrix, as they are never on the public listener |
| Sasonica Shell | relay.sasonica.com | **never** over this. Umbrella rule 1: the link never carries the shell |

An answer bigger than one event (a `/conversation/log` page with tool
output) is gzipped and split into numbered parts under 60 KiB; a homeserver
refuses an event over 64 KiB. Past a few parts, the body goes to the media
repository and the response carries its `mxc://` instead.

## Who runs the homeserver

Three modes. The app treats them the same: the pairing link names a
homeserver and a user, and nothing in the app names sasonica.com.

1. **The user's own homeserver.** David's case: red5 and p8a on ryer.org.
   No Sasonica account is involved at all. The same is open to anyone who
   already runs Matrix.
2. **matrix.sasonica.com, hosted.** For people with a Sasonica account,
   signing in through Drupal's issuer as the 29 Sep split says. It is
   transport only, which keeps it small: federation off, registration only
   through a Sasonica sign-in, no public rooms. That removes most of the
   moderation and abuse cost that counted against it on 4 Oct. The software
   is tuwunel, as on ryer.org, if its OIDC login can take Drupal as the
   upstream; otherwise Synapse with MAS.
3. **No Matrix.** A direct address, or the quick tunnel plus lookup.

So **accounts stay optional**. Matrix is never required, and a self-hoster
either brings a homeserver or uses the lookup. The one thing not offered is
an anonymous, install-minted user on sasonica.com's homeserver. That would
be the lookup's "never logs in" property again, but with an open
registration endpoint to defend. The lookup already does that job for a few
bytes of KV.

## Does the address problem go away?

**For the routes Matrix carries, yes.** Both ends dial out to a name that
never changes (`sasonica.com`'s `.well-known` delegation, or the user's own
homeserver), so a quick tunnel's restart, a reboot or a new network means
nothing. With every app route behind the request envelope, the server needs
no inbound path, no cloudflared and no public listener. That also suits the
server on the phone (`2026-09-27-server-on-the-phone.md`), where cloudflared
is awkward.

**It moves, rather than vanishes, in two cases:** a self-hosted homeserver
needs a public address of its own (a problem homeserver operators have
already solved), and large uploads still want a direct path. And it buys
nothing for a no-account, no-homeserver user. That is the lookup's case,
which is why the lookup should come back as a permanent part of the design,
not a stopgap.

## Encryption, and what sasonica.com can see

To-device events can be Olm-encrypted (`m.room.encrypted`), and room events
Megolm-encrypted. With both on, the homeserver sees **metadata only**: which
devices of which user talk, when, how often and how big; that the event
type is "encrypted"; and the push metadata (`event_id_only` pushes carry no
content). It cannot see routes, prompts, transcripts, speech text or files
(attachments are encrypted on the client before upload). Today's
alternatives are weaker: the hosted relay terminates TLS and stores what it
carries, readable by the operator, and the quick tunnel runs through
Cloudflare's edge in the clear.

The price is a real crypto client at both ends. The canvas's Matrix code
(`agent_media_core.matrix`) is standard library only and cannot do Olm. The
server needs vodozemac's Python bindings or the rust SDK, built for all
three single-binary targets. The app needs the rust SDK's Android bindings
in the Java layer, because NotifyService syncs while the WebView is gone.
That adds tens of MB to the APK. Proposed: **unencrypted on David's own
homeserver for the first steps** (he is the operator; ryer.org's `sam` room
is unencrypted today), and **E2EE required before sasonica.com carries
anyone's traffic.**

## Latency, speech first

`speech-latency-notes.md` is the baseline: submit to audible was about
6.8 s on 21 Sep. 4.3 s of that was on red5 (the broker claim and
`before_speech`'s pause and duck round trips), and of the phone's 2.45 s,
**about 0.4 s was the command in transit**. The rest was the clip fetch and
the decoder. Since then speech frames removed the per-call IPC: reads are
answered from the frame endpoint's model, and only commands and state
reports cross the link. `RENDER_SASONICA=device` sends `tts:` text, so no
audio crosses at all.

Over Matrix, a frame is one PUT from red5 to the homeserver, then the
phone's waiting `/sync` returning. Against today's SSE through the tunnel,
that is one more hop, plus the phone re-issuing `/sync` before the next
batch (frames sent close together arrive together). Expect it to add tens to
low hundreds of ms per frame. That is small next to the 6.8 s, but it sits
on the one leg that was already fast, so **measure start-of-speech in step 1
before deciding**. A request/response route costs two such hops (request
up, response down), so expect roughly 0.2–0.4 s against the tunnel's
0.06–0.09 s. That is fine for loading a list and noticeable for the speech
bar's controls. Rule proposed: where a direct address answers, the app uses
it and Matrix is the fallback. Where nothing else answers, Matrix is the
path.

Both ends must sit on **one homeserver** (one user, two devices), never
across federation, which adds seconds.

## Uploads and media

Speech needs no media path (text only). Shared files do. Through the
homeserver, a file is encrypted on the phone, uploaded as ciphertext to the
media repository, and named by `mxc://` in the request. The server fetches
it, decrypts it and writes it under `~/shared/` as `/upload` does today, and
`GET /upload` goes back the same way for the thumbnails. Limits: the
homeserver's upload cap, not 512 MB (say 50 MB on sasonica.com), and a
retention sweep there (30 days, as the relay keeps), because the client API
cannot delete media. A larger file waits for a direct path, and the app says
so. `/img/` pictures are small and go as media too.

## Pairing

The desk still decides what a device is: `sasonica pair --device NAME`, a
one-time code, 30 minutes, as in §9.

1. The server joins Matrix once: `sasonica matrix login` (own homeserver:
   the user's password typed at the terminal, as `matrix-owner-login` does
   now; hosted: a Sasonica sign-in in the browser). The server becomes a
   device, "Sasonica server · red5", of that user.
2. The pairing link gains `hs=<homeserver>`, `mx=<user id>` and
   `dev=<server's device id>` beside `server=` (which may be absent) and
   `code=`.
3. The phone signs in to the same user: the Sasonica account it already
   offers, or the homeserver's own login. It sends `POST /pair {code}` as a
   request to `dev` and gets its §9 device token, as today.
4. With E2EE on, the same QR then verifies the two devices to each other
   (cross-signing), so the server answers only a verified phone, and the
   phone talks only to the server it scanned.

So an account still only enrols; it does not authorise. Someone who takes
over the Matrix account gets a device that can send requests, but not a §9
token. The 29 Sep line "accounts enrol devices, they do not authenticate
requests" holds unchanged.

## Order of work

Each step keeps the old path working, and the app falls back to the next way
in when one fails.

0. **Now, unrelated to Matrix:** revive the lookup as the permanent
   no-account path (today's handover). Nothing below waits for it, and it
   does not wait for this.
1. **red5 ↔ p8a on ryer.org, unencrypted, frames only.** The server logs in
   as a device of `@david`; NotifyService holds a `/sync` instead of
   `/sessions/events` when the server advertises Matrix; frames arrive as
   to-device events. Measure start-of-speech against the SSE path and the
   21 Sep table. Stop here if it costs more than about 0.3 s.
2. **The request envelope**, dispatched into `app.dispatch`, with chunking
   and the response cache. The app's fetch layer gains a Matrix transport
   underneath. Test with the tunnel switched off: the phone should work
   with no inbound path at all.
3. **Pairing over Matrix** (above) and the link's new keys; `sasonica
   matrix login` in the installer for own-homeserver users.
4. **E2EE at both ends**: the native dependency in the three server builds
   and the APK, plus verification at pairing. Required before step 5.
5. **matrix.sasonica.com**: a closed homeserver signing in through Drupal,
   `sasonica install --hosted` offering it, media cap and retention.
6. **Threads as rooms**, the 29 Sep idea: a thread's messages as Megolm
   room events, so Element and other clients can read them and a push
   gateway can wake a killed app. This is separate from the transport and
   only worth doing once 1–5 are carrying real traffic.

## Open questions for David

1. Is the lookup now permanent, for no-account users, rather than
   "throwaway once Matrix lands"?
2. Should matrix.sasonica.com require a Sasonica account (proposed), or
   also mint anonymous per-install users?
3. E2EE required on sasonica.com from day one (proposed), even though it
   means native crypto in the Windows/macOS/Linux binaries and a larger
   APK?
4. Where would matrix.sasonica.com run (red4, a VPS), and on tuwunel or
   Synapse with MAS?
5. Push when the app is killed: FCM through Sygnal (a Google and Firebase
   dependency), UnifiedPush (ntfy), or keep NotifyService's held connection
   as today?
6. Threads as rooms (step 6): do you want agent threads readable in
   Element, given their history would then live on the homeserver
   (encrypted)?
