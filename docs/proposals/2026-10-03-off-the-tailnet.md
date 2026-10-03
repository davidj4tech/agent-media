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
2. **Done 3 Oct: a public listener.** The canvas has a second bind
   (`--public HOST:PORT` / `MEDIA_VISUAL_PUBLIC`; on red5 `127.0.0.1:8789`
   via the unit drop-in `public.conf`). `PublicHandler` answers
   `app.dispatch` (every app route and nothing else), `/img/` pictures and
   `/healthz`. Everything else is a 404. It takes the caller from
   `CF-Connecting-IP` / `X-Forwarded-For`, believed only from a loopback
   peer, so rate limits and `last_ip` see the phone. It was tested through a
   quick tunnel: app routes at 0.1–0.2 s, and the desk page, `/peek` and
   `GET /pair` came back 404. Found on the way, and fixed on both listeners:
   `GET /img/pair-code` served the pairing code that unlocks the amux token
   (and `last-clip.json` and the scene lists), because the spool holds them
   too. `/img/` now serves picture suffixes only.
   That means no `GET /pair` (it hands out the amux token), no `/peek` (a
   pane's conversation, unauthenticated), no `/speech`, `/agents` or
   `/events`. cloudflared points at this listener only. The allowlist lives
   in code, not in a tunnel config that would drift.
   The app loads `/img/<name>` (public, above). Clip URLs on `:8780` are not
   needed under `RENDER_SASONICA=device`, which sends `tts:` text, not audio.
3. **Done 3 Oct: cloudflared on red5.** `~/.local/bin/cloudflared`
   (2026.9.3, from Cloudflare's GitHub releases). Named tunnel
   `sasonica-red5` (`9518053b-…`), set up by David's `cloudflared tunnel
   login` on sasonica.com. `~/.cloudflared/config.yml` sends
   `red5.sasonica.com` to 127.0.0.1:8789 and everything else to a 404. The
   user unit is `sasonica-tunnel.service` (linger is on). Over it, the app's
   routes answer in 0.06–0.09 s. Two fixes it needed:
   - `pair --device NAME --server https://red5.sasonica.com`
     (`MEDIA_VISUAL_PAIR_SERVER`). A link used to always be
     `http://host:port`.
   - `/sessions/events` caps `?ping=` at 60 s when the request came
     through a tunnel or proxy (`CF-Connecting-IP` / `X-Forwarded-For`).
     Cloudflare cuts a connection after 100 s idle, and the phone's
     background stream asks for 120.
4. **Move p8a** to the https name. Re-pairing is not needed: it is the same
   server, so the device token holds. Settings → Advanced → Server address
   → `https://red5.sasonica.com` → Save. Keep the tailnet pairing as a second
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
