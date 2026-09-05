# Reply from the player

Status: proposal, nothing built. 2026-09-04, out of the session that put
Sasonica on the phone. The idea in one line: **while listening to a
conversation in Sasonica, type a reply and have it land in the live session.**

This would be the first change that exists only in the fork. Everything on the
`sasonica` branch so far is either upstream's (#2010) or cosmetic (the name, the
debug keystore); all of it rebases. A reply box that posts to agent-media does
not, and that is the point — it is the first step of the absorption path in
[[abs-fork-direction]], where the fork takes over the companion app's jobs.

## Why it is cheap

Both halves already exist. Nothing here needs a new service.

**The inbound path is built and authed.** `POST /input` on the canvas
(`packages/visual/.../canvas.py:1507`) types text into a Claude Code session.
It takes `{"text": ..., "target": ...}` where target is `speaker`,
`amux:<name>` or `tmux:<pane>`, and `send_input` (canvas.py:686) validates the
target against live panes before typing — a bare shell pane is refused, because
typing text and Enter into one is host command execution. Auth is the amux
token (`X-Auth-Token` or a bearer), or nothing at all when
`MEDIA_VISUAL_TRUST_TAILNET=1`. This is the same surface the Open WebUI pipe
uses, so the wire format is already proven by a second caller.

**The item→session mapping is already on disk.** Each
`~/.local/state/agent-media/book-tracks/<id>.json` carries `session` and
`folder`, and the folder is exactly what ABS scanned into the `Conversations`
library. Ten manifests exist today. So "which conversation am I hearing" →
"which session is that" is a lookup, not an inference.

**And session→pane is already in the state store.** Speech history rows carry
`extras.source_pane` alongside `extras.source_session`; `_last_speaker`
(canvas.py:517) already walks them and probes each pane with `_pane_alive`
before trusting it. Resolving a *named* session instead of the most recent one
is the same walk with one more predicate.

That is the whole spine: a Vue input, a resolver of maybe ten lines, and an
existing endpoint.

## Shape

| piece | where | size |
| --- | --- | --- |
| reply box on the item page | `pages/item/_id/index.vue` (Sasonica) | small; same file #2010 touched |
| only show it for conversations | library id check, client side | trivial |
| `session → pane` resolver | new `target` form on `send_input` | ~10 lines |
| `item → session` lookup | reads the book-tracks manifests | small, new endpoint |
| the token on the phone | see below | the actual open question |

Note the app side is **web, not Kotlin**: the ABS app is Capacitor, so the item
page is Vue in a WebView. No Android build knowledge is needed to write this,
which is the opposite of what the job sounds like.

The natural target form is `session:<uuid>` — resolve the uuid to a live pane
through the speech history, refuse if the pane is gone. That keeps the "never
type into a non-Claude pane" guarantee that `send_input` already enforces,
rather than inventing a second, weaker path to the same keystrokes.

## The open question: the token

`/input` is keystroke injection, so it is gated, and the gate is a 40-char amux
token. The canvas page solves this with `/pair?c=<code>`: a one-time code minted
host-side drops the token into the browser's localStorage (canvas.py:319). The
Sasonica WebView is a different origin with different storage, so it does not
inherit that, and three options present themselves:

1. **Reuse `/pair` inside the app** — a settings screen that takes a pairing
   code once and keeps the token in Capacitor storage. Most consistent with
   what exists; most work.
2. **Trust the tailnet** — `MEDIA_VISUAL_TRUST_TAILNET=1` on red5 and post with
   no credential. One env var, and it widens the surface: anything on the
   tailnet can then type into any session. Fine for a single-user tailnet,
   worth stating out loud rather than defaulting into.
3. **Ride the ABS token** — the app already holds a server credential. But that
   authenticates to Audiobookshelf, not to agent-media, and reusing it would
   mean agent-media trusting ABS's session model. Mentioned to be dismissed.

(1) is right; (2) is the honest shortcut for a first cut, and reversible.

**"Do we need the token at all — you already log in to see the library?"**
(David, 2026-09-05.) The login is a real gate, but on the wrong door: it
authenticates a user to *Audiobookshelf*, while `/input` is on the canvas, a
different service on red5:8781. The reply POST goes phone→canvas and never
touches ABS, so an ABS session grants nothing there; anything on the tailnet can
call it either way.

The objection still lands, though, because the token is **not a login**. It is a
shared secret the app can carry silently — set once in config, never typed,
never shown. What is worth avoiding is a second credential *in the user's face*,
and that is achievable without opening the endpoint.

What the token is still worth is narrower than it looks. The canvas binds to the
Tailscale IP (verified: `LISTEN 100.103.43.93:8781`), so the network is already
a gate, and the token's stated job in canvas.py:308 is CSRF — a site your
browser visits POSTing keystrokes into your agents. A Capacitor WebView showing
the ABS app does not browse arbitrary sites, so that threat is close to absent
*for the app*. But `MEDIA_VISUAL_TRUST_TAILNET` is global: dropping the token
for Sasonica drops it for every browser on the tailnet too, which is exactly
where the threat does live.

So: keep the token, provision it silently. Identical friction to no auth, and it
does not widen the browser path to get there.

**Superseded** — see "The ABS login *can* be the credential" below. The phone
sends its ABS bearer and the canvas verifies it with ABS, so no secret is
provisioned to the phone at all. The token stays, unchanged, for the browser
and the Open WebUI pipe.

## What it should not do

Not a chat UI. The conversation's representation is audio — clips as tracks,
turns as chapters — and the reply box is a *way in*, not a second transcript.
The answer comes back the agent-media way: spoken, on whatever target is
current, and appended to the same growing item. If the reply rendered as text in
the app, there would now be two representations of one conversation to keep in
step, which is exactly what retiring the mirror was meant to end.

## Unknowns

- ~~**What happens when the session is dead.**~~ Answered below.
- **Whether the item id reaches the resolver cleanly.** The lookup is keyed on
  the folder path; whether the app can hand over something that maps to it
  without a fragile title match is unverified.
- Whether the reply should pause playback while you type.

## Dead sessions: resume them through tmux

(David, 2026-09-05: "if a session doesn't exist anymore it should be resumed
through tmux.") This is right, and it is much smaller than "start a fresh
session seeded with the reply" made it sound — because resuming a Claude Code
session in a tmux pane is already a solved, scripted thing here.

`claude-resume --print` emits one row per resumable session with exactly the
four fields the resolver needs: uuid, live flag, pane id, and cwd. Live
detection is honest — it reads `--resume <uuid>` out of argv for resumed panes
and the `SessionStart` hook registry at `~/.claude/tmux-sessions/<pane>` for
fresh ones, driven off running processes, so stale registry rows do not lie.

So the resolver's `session:<uuid>` target stops being pane-or-refuse and
becomes pane-or-*revive*:

1. Row is live (`●`) → type into that pane. Unchanged from the current plan.
2. Row exists but is dead → open a window and resume it, then type into the new
   pane.
3. No row at all → refuse. That is a genuine miss, not a cold session.

Case 2 has one hard constraint, learned the hard way and recorded in
[[amux-start-needs-client]]: **Claude Code's TUI needs a tmux client attached at
launch.** It cannot be spawned detached — the recipe that works is a
`new-window` in the already-attached session:

```
tmux new-window -t projects-agent-media -c <cwd> \
  "exec env -u ANTHROPIC_API_KEY claude --resume <uuid>"
```

`exec` so quitting claude closes the window rather than dropping to a shell.

Two things then need care. **The startup race**: the reply cannot be typed until
the TUI is up and taking input, so the resolver has to wait for readiness rather
than sleep-and-hope — `send_input` already probes panes, and the same probe can
be the gate. **Double-resume**: two instances on one session interleave writes
into the same transcript, and the `SessionStart` resume guard already catches
this — it switches focus to the rightful pane and warns both sides. Checking the
live flag first means we never provoke it, but it is a floor under a race, which
is reassuring.

### What this changes about the token

It sharpens the argument for keeping it. Until now a reply POST typed into a
pane that already existed; now a phone tap can *spawn a process* on red5, with
a cwd, at a session's own working directory. Still only `claude --resume` on a
uuid we already have a manifest for — not arbitrary execution — but it is a
real escalation of what the endpoint does, and the answer above holds: provision
the token silently, do not open the endpoint.

## The ABS login *can* be the credential — if the canvas verifies it

(David, 2026-09-05: "the account login to sasonica is sufficient... maybe we
can have different levels of access. the root account should have access to
input by default.")

I dismissed this above as "reusing an ABS credential would mean agent-media
trusting ABS's session model". That was too quick, and the difference it missed
is the whole design: **reusing a token blindly is not the same as verifying it
with its issuer.**

The app already holds an ABS bearer, and ABS already exposes the exact check
the app itself does at startup — `POST /api/authorize` with that bearer, which
returns the user object (`ServerConnectForm.vue:923`, `layouts/default.vue:166`,
`ApiHandler.kt:727`). So the canvas can stop guessing:

```
phone --(ABS bearer)--> canvas /input
                          canvas --(same bearer)--> ABS POST /api/authorize
                          <-- {user: {username, type: root|admin|user|guest}}
                          policy: may this user type into a session?
```

No ABS fork — this is stock ABS answering a question it already answers, which
keeps [[abs-fork-direction]]'s finding ("the server needs no fork") intact. No
second credential in the user's face: the login you already did *is* the
authorization. And no amux token on the phone at all, which is strictly better
than provisioning one silently — a secret that never leaves red5 cannot leak
off it.

The amux token stays accepted alongside, for the canvas page and the Open WebUI
pipe. Two accepted credentials, one per caller class, rather than one stretched
over both.

### Levels

`user.type` is `root` / `admin` / `user` / `guest` — the app already reads it
(`store/user.js:20`). But ABS has no permission that means "may type into an
agent", and bending an existing one (`permissions.update`, say) into that
meaning would be a lie that outlives whoever wrote it. So the levels live in
agent-media config, keyed on ABS username:

- **root: allowed by default.** On a single-user server root is you, and making
  the sole admin edit a config file to talk to their own agents is friction
  with no security gained. Configurable off, for the day that stops being true.
- **everyone else: denied by default**, allowed by explicit username. Not by
  type — `admin` is a library-management role, and someone you trust to fix
  metadata is not thereby someone you trust to type into a shell-adjacent
  session.

Denial should be visible in the app ("this account can't reply"), not a silent
no-op.

### What it costs

- **The canvas now depends on ABS being reachable** to authorize a reply. Mild:
  if ABS is down you were not listening to the conversation anyway.
- **A round trip per POST.** Cache the `authorize` answer per token for a
  minute or so; replies are typed by a human, so the rate is trivial.
- **A bearer is not auto-attached by browsers**, so unlike a cookie this adds
  no CSRF surface — the reason the amux token exists (canvas.py:308) is
  untouched for the callers that still use it.

### Not a new user type

(David, 2026-09-05: "should we make another user type? Or some kind of
read-only option?")

No to the type, and read-only already exists.

**A new ABS user type is a server fork**, and a permanent one. Types are not a
client-side label — they are validated on user creation, drive the server's own
permission checks, and appear in the admin UI. Adding `agent` or `replier`
means diverging the ABS user model, carrying a migration, and touching that
divergence on every rebase, in exchange for what is fundamentally one boolean.
The whole finding of [[abs-fork-direction]] is that stock ABS is enough; this
would spend that for very little. (The app's own reading is coarser still —
`store/user.js:21` only asks admin-or-up — so a custom type would read as a
plain user in the client regardless.)

**Read-only is the status quo.** Every ABS account today can only listen; the
reply box adds exactly one verb on top. So the axis isn't read vs write, it's
*may this account inject input* — a single capability, and one whose meaning
lives entirely in agent-media, not in ABS. If you want a genuinely
listen-only account — a guest, a lent-out phone — ABS already gives you that
upstream via the `guest` type and per-library access, without us building
anything.

**So: a capability, not a role.** agent-media is the thing being typed into, so
it owns who may; ABS only answers "who is this". That also scales the way this
is going to grow — the app will plausibly want to pause an agent, or answer a
prompt, later. Those are more verbs, and verbs go in a capability list. Adding a
user type per verb does not survive the third one.

The one honest argument for the fork is management in one place: a checkbox in
the ABS admin UI beats a config file on red5. Worth wanting, not worth a
permanent divergence yet — and if it ever is, the capability list is what the
checkbox would set anyway.

