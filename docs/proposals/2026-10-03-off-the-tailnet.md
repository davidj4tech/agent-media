# Sasonica off the tailnet (3 Oct 2026)

David, 3 Oct 2026: "I'd like to move away from the dependencies on tailscale
for sasonica". Chosen: **Cloudflare Tunnel** for phone → server (contract
§19 shape B), and **the phone dials out** for everything red5 now opens into
the phone. Prompted by the Organiser hanging while p8a's tailnet link was
slow or down (red5 answered in ~1 ms; a ping took 422 ms over mobile data).

## Does it work for other people?

Yes, and the design is the same for each of them: every user runs their own
server and their own tunnel, and the app stores whatever base URL the
pairing link carried (§9, §19). Nothing in the app names a host. The
hostname is per-install:

- **No account, no domain:** `cloudflared tunnel --url …` (a "quick tunnel")
  gives a random `*.trycloudflare.com` name. Fine for trying it out. The
  name changes on every restart, so the app would need re-pairing; not a
  lasting answer.
- **Their own domain on Cloudflare:** a named tunnel, a stable name. This is
  the documented path.
- **The long-term path (David, 3 Oct 2026): Sasonica does it.** A small
  service on `sasonica.com` that the user never logs into, so a stranger
  installs the server, pairs the phone, and never sees Cloudflare. There
  are two versions, and the cheaper one comes first:
  - **Lookup.** The server runs a free quick tunnel and, on every start,
    POSTs its current `*.trycloudflare.com` URL to
    `sasonica.com/r/<install id>`, signed with a per-install key minted at
    install. The app stores the install id (the pairing link carries it) as
    well as the base URL. On a failed request it asks the lookup for the
    current URL, then retries. The service is a Worker with one KV entry per
    install. It sees addresses only, never traffic. The risk: quick tunnels
    are best-effort and have no SLA, which is fine for a lookup but worth
    watching.
  - **Relay.** The server holds an outbound WebSocket to a Durable Object
    per install, and the app talks to `<id>.sasonica.com`. This gives a
    stable name with no cloudflared at all, which fits the server on the
    phone (2026-09-27 proposal), where running cloudflared is awkward. The
    costs: Sasonica carries the traffic (speech and SSE are small, uploads
    are not), it terminates TLS for the user's data, and it is a real
    service to run.
  - Both use the same server-side piece: the app-routes-only public
    listener (step 2 below). Only what points at it changes.

red5's own name is only David's choice: a named tunnel on his domain now,
and the lookup can come later without changing the server's routes.

## Phone → server: the tunnel

1. **Done 3 Oct:** desk-route hardening owed by §19, in `visual/canvas.py`.
   The amux token is now compared with `hmac.compare_digest`. `/show`,
   `/ctl`, `/say`, `/play`, `/input` and an explicit `/seen` screen get 10
   failures per source per 10 min. The app routes are not limited, because
   they ask the amux check before the device token.
2. **A public listener.** The canvas gets a second bind (`--public
   127.0.0.1:<port>`) that answers app routes only: `cors_path()`, `POST`
   for `CORS_POST_PATHS`, and `/threads/<id>/…`. Everything else is a 404.
   That means no `GET /pair` (it hands out the amux token), no `/peek` (a
   pane's conversation, unauthenticated), no `/speech`, `/agents` or
   `/events`. cloudflared points at this listener only. The allowlist lives
   in code, not in a tunnel config that would drift.
   Open: does the app load `/img/<name>` or clip URLs on `:8780`? Each one it
   does is either added to the public set or moved onto the stream.
3. **cloudflared on red5.** A user unit, a named tunnel, and a DNS CNAME.
   Needs `cloudflared tunnel login` once (David, in a browser) or a token
   with Cloudflare Tunnel: Edit plus DNS: Edit. The existing install-token
   has neither.
4. **Re-pair p8a** to the https name. Keep the tailnet pairing as a second
   device until the tunnel has carried a few days of use.

## Server → phone: the phone dials out

Today red5 opens connections into p8a by its MagicDNS name. Normal use,
worst first:

| # | What | Today | Instead |
| --- | --- | --- | --- |
| 1 | Speech: mpv JSON-IPC, `tts:` text under `RENDER_SASONICA=device` | `ipc_relay` → `p8a:6614` (3 idle spares, renewed every 45 s) | a `speech` frame down `/sessions/events`; position/state POSTed back |
| 2 | Music on the phone, plus ducking | `tcp://p8a:6615` | a `music` frame; the app POSTs now-playing |
| 3 | "Is the companion there?" | `ssh p8a` on **every reply** (`route/_android.py`) | delete it: the answer is always "companion", so nothing happens |
| 4 | Book state/control (old ABS app) | ssh + `curl 127.0.0.1:8772` | the app reports; or retire it with the ABS app |
| 5 | Notifications (converse question, missed replies) | ssh → termux-notification | a frame, as `phone` and `mic` already are |
| 6 | Books cached on the phone | `ssh p8a find` | the app reports its cache |
| 7 | Radio handoff to Spotify | `tcp://p8a:6617` (unmerged `handoff` branch) | a `radio` frame |

Dev-only (doctor, audiobook-fetch, companion deploy) and legacy paths
(ABS 6613/8773, Termux bridges 6601–6603, say-http 8790) stay as they are
or go with their apps. One literal phone IP: the ABS quadlet's whitelist
(`deploy/quadlet/audiobookshelf.container:33`).

**The cost to watch:** the relay exists to hide per-call latency on speech
(#1). A frame on an open stream should be no slower than a warm socket,
but measure start-of-speech before and after; `docs/speech-latency-notes.md`
has the baseline.

## Order

1 (done) → 2 → 3 → 4, then #3 (a deletion), #5, #1, #2, #4/#6, #7. Each
server → phone item ships with the old path kept as a fallback until the
app build carrying the frame is on the phone.
