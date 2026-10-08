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

**For other users, 8 Oct 2026.** Built: the quick tunnel above, set up by
`sasonica install` (`media-tunnel`, packages/server/.../tunnel.py; service
`sasonica-quick-tunnel`), and `pair --device` naming its https URL. The
**lookup is on hold pending Matrix** (David, 8 Oct 2026: a sasonica.com
homeserver may replace it), so a quick tunnel's restart means pairing the
phone again (or typing the new address into Settings → Server address: the
device token holds across names). The lookup's design as built and tested
locally — the id is base32(sha256(public key)[:16]), so the first write
cannot be squatted; `PUT /r/<id>` `{url, ts, pub}` signed over the raw body;
only `https://*.trycloudflare.com`; 1 KB; 20 writes/min per address, 5 s
apart and 30/hour per id; entries live 60 days, republished daily — is on
agent-media branch `lookup-hold` (deploy/lookup), and the app half on
sasonica-app branch `tunnel-lookup`. It was deployed for a few minutes on
8 Oct and taken down again when the hold came.

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
4. **Done 3 Oct: p8a moved** to the https name. The device's `last_ip` is
   now its public address (via `CF-Connecting-IP`). cloudflared logs `ERR …
   canceled by remote` each time the app closes a thread stream; that is
   noise, not a fault. Re-pairing is not needed: it is the same
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
| 4 | Book state/control (old ABS app) | ssh + `curl 127.0.0.1:8772` | **done 8 Oct:** the Termux worker runs the same curl (5143153); retire with the ABS app |
| 5 | Notifications (converse question, missed replies) | ssh → termux-notification | **done 8 Oct:** a `notes` frame (§6.23; 2bd60c3, app 44c846b) |
| 6 | Books cached on the phone | `ssh p8a find` | **done 8 Oct:** the Termux worker runs the `find` (5143153) |
| 7 | Radio handoff to Spotify | `tcp://p8a:6617` (unmerged `handoff` branch) | a `radio` frame |

Dev-only (doctor, audiobook-fetch, companion deploy) and legacy paths
(ABS 6613/8773, Termux bridges 6601–6603, say-http 8790) stay as they are
or go with their apps. One literal phone IP: the ABS quadlet's whitelist
(`deploy/quadlet/audiobookshelf.container:33`).

**8 Oct: #4, #5, #6 done, and an audit of what still dials p8a.** #5:
`POST /notes` (host token) → a `notes` frame on `/sessions/events?notes=1`
(contract §6.23, server notes.py, sasonica-app 44c846b); the converse
doorbell and the missed-speech note post there, and keep ssh only while no
app has ever asked for notes (`notes-seen` stamp). Verified: a test note
reached the shade on the Sasonica alerts channel and came down on clear.
#4 and #6 go through the phone-jobs worker (`music_local.phone_argv`), which
is the same `curl`/`find` run in Termux, dialled out; the live `search` found
the phone's cached `agenda`. Found by the audit (15 min of SYNs and ssh
processes, plus a traced duck): **every reply's music duck dialled
p8a:8773 (old ABS player) and p8a:6601 (Termux mpv)**, both unused since
28 Sep. `MEDIA_PHONE_PLAYER_URL_ABS` and `MEDIA_MUSIC_LOCAL_ENDPOINT` are
commented out in `~/.config/agent-media.env` (dated, with how to revert);
the duck's resolve went from 0.75 s to 0.01 s. Still dialling in: the
relay's spares to p8a:6614 (a SYN every ~14 s; the speech frames' fallback),
music frames' pass-through to p8a:6615 (only with no frame device),
`media music now` trying `tcp://p8a:6617` (#7, refused: the handoff app is
not built), the rooms lane's YouTube fetch (`music_fetch._phone_fetch`, raw
ssh; last landed 30 Sep), `media doctor`, and, outside agent-media,
`agent-sessions sync` (rsync over ssh every few minutes) and Claude's
`browser-p8a` MCP (a standing `ssh p8a playwright-mcp-headless`).
`media-lane` is in forced `phone` mode, so it does not probe.

**#3 done 3 Oct:** `MEDIA_ANDROID_PAUSE_HOSTS=p8a` is commented out in
`~/.config/agent-media.env` (confirmed: `ssh p8a curl 127.0.0.1:8774/state`
answers, so every probe ended in "companion"). The code stays for other
Android hosts.

### Speech as native frames (#1; David, 3 Oct 2026: frames, not a reverse pipe)

The server side speaks mpv JSON-IPC to the phone from about 40 call sites
(`sinks/speech.py`, the follow loop in `intake/submit.py`, `cli.py`'s
transport). The phone's `MpvServer` implements the subset they use. Most of
the traffic is the follow loop's 8-property snapshot, polled every tick at
about 1.3 s a round trip.

**Shape.** The callers stay as they are. A **frame endpoint** in the canvas
process (`speech_frames.py`, on 127.0.0.1:16624) passes through to the relay
on 16614 while no frame device is connected. It speaks mpv
JSON-IPC to the callers, but owns the player state itself:

- **Commands become frames.** A `speech` frame goes down `/sessions/events`
  to the paired device that plays speech, pushed with `poke()`, not the 3 s
  tick. The set:
  - `play {reply, sentences:[{id,text,voice,fallback}], start, title, priority}`:
    replaces claim + audio-device + gapless + stop + clear + load + start.
  - `append {reply, sentences, final}`
  - `ctl {reply, op: pause|resume|stop|goto|seek|speed|volume|mute}`
  - `meta {reply, text|speaking|priority}`: the user-data the app's Holds
    read.
  - `cue {name}`
- **Reads are answered locally**, from the last state the phone reported,
  with commands applied optimistically (a `playlist-pos` set reads back at
  once). The follow loop's snapshot then costs nothing on the link.
- **The claim/owner lease** (`am-claim-play`, `user-data/am-owner`) is kept
  in the endpoint, because there is one server now.
- **State comes back:** `POST /speech/state {reply, state, i, time_pos,
  duration, count, speed, mute, volume, ringer, at}`. The app sends it on
  every transition (item change, pause, end, error, stop at the phone) and
  as a heartbeat about every 2 s while playing. It also carries the
  ringer, which `read_ringer` asks for.
- **The app:** NotifyService hands each `speech` frame to SpeechService. It
  feeds the frame to the same playlist/Media3Speech code MpvServer drives,
  so playback, TtsClip rendering, ClipCache warming and focus do not change.
  MpvServer stays as the fallback until the frame build has proved itself.
- **Switch:** `MEDIA_SPEECH_FRAMES_LISTEN=127.0.0.1:16624` and
  `MEDIA_SPEECH_FRAMES_UPSTREAM=127.0.0.1:16614` start it.
  `MEDIA_SPEECH_SOCKET_SASONICA=tcp://127.0.0.1:16624` points the callers at
  it. The app advertises `speech=frames` on its
  stream, so the endpoint knows a device can take frames.

**Fallback retired 9 Oct (David).** The pass-through to p8a:6614 had been
refused since at least 6 Oct (about 950 failed connects a day) while speech
went through frames throughout; `agent-media-speech-relay.service` is
stopped and disabled, so nothing dials p8a for speech. Revert:
`systemctl --user enable --now agent-media-speech-relay.service`.

**Live 3 Oct.** Server: ee8f975, cf69e0a, 89626df. App: cac6e3d (build
1154). A test line played through 16624: the phone reported pos 0 to idle
over about 4 s. Then `MEDIA_SPEECH_SOCKET_SASONICA` moved to 16624, and the
five services that read it were restarted while speech was idle. Still to
measure: start-of-speech against `docs/speech-latency-notes.md`, and
follow-along on a long reply.

**Music (#2), live 3 Oct.** The same module, now one hub per player channel
(`CHANNELS`: speech, music). Music has its own port (16625, passing through
to p8a:6615), `music` frames on `?music=frames`, and `POST /music/state`.
App: `SpeechFrames.SPEECH` / `.MUSIC` (sasonica-app 867cd8c, build 1157).
`MEDIA_MUSIC_SASONICA_ENDPOINT=tcp://127.0.0.1:16625` since 3 Oct.
**The Termux work, live 3 Oct (David: the YouTube download must stay on
the phone; red5's data-centre address is blocked; a Termux worker, not the
app).** `phone_jobs.py` + `agent_media_core.phone_run` + the
`deploy/phone/service/phone-jobs` worker (paired as "p8a Termux jobs",
`d_8694155241ee`; copied into `$PREFIX/var/service`, as music-files is).
`MEDIA_PHONE_JOBS=1` sends every `music_local.phone_argv` command there:
cached?, fetch, title, chapters, the radio's mix and search. A job takes
about 0.6–0.8 s, as a warm ssh did. The worker sends its own User-Agent,
because Cloudflare refuses `Python-urllib` (error 1010). Left on ssh: the
`abs` fallback's raw `ssh` in `music_fetch.py`, and the stdin seeding of the
Termux player. Both are fallbacks only.

**The cost to watch:** the relay exists to hide per-call latency on speech
(#1). A frame on an open stream should be no slower than a warm socket,
but measure start-of-speech before and after; `docs/speech-latency-notes.md`
has the baseline.

## Order

1 (done) → 2 → 3 → 4, then #3 (a deletion), #5, #1, #2, #4/#6, #7. Each
server → phone item ships with the old path kept as a fallback until the
app build carrying the frame is on the phone.
