# Proposal: split the app's API out of `visual` (21 Sep 2026)

Status: **proposal, nothing moved.** Plan step 4 in `simplification-plan.md`;
the contract it serves is `docs/server-contract.md`.

## Why

`visual` began as the canvas — a picture page with an SSE hub — and became
the app's server by accretion. Today `canvas.py` (2,325 lines) holds the
page, the image hub, the video poller, the speech snapshot, pane
classification, keystroke injection and the whole route table. `reply.py`
(2,281 lines) holds ABS identity, session discovery, revival, ask routing,
drafts and the conversation log. Two consequences:

- The app's contract cannot be read off any one file, which is why
  `server-contract.md` had to be written from both.
- `reply.py` reaches back into `canvas.py` for pane mechanics
  (`_classify_agent` ×4, `_strip_ansi` ×4, `_send_to_pane` ×3, `_pane_alive`
  ×3, `AGENT_COMMANDS`, `_tmux_cc_panes`, `_herdr_cc_panes`,
  `_agent_by_argv`). The dependency runs in both directions.

## Shape

A new package, `packages/server` → `agent_media_server`, depending on
`core`. `visual` depends on it, not the other way round.

```
agent_media_server/
  app.py         route table, envelope (§3/§13), CORS, body cap, one Handler
  auth.py        the gate: device tokens (§9), then the ABS adapter
  auth_abs.py    abs_urls / abs_identity / _identity_error / may_reply   ← reply.py
  devices.py     pairing codes, devices.json, revoke                     (new)
  panes.py       send, capture, classify, strip_ansi, alive, agent panes ← canvas.py + visual/panes.py
  sessions.py    live_sessions, sessions_index, places, session_states,
                 transcript_cwd, approval_for / parse_dialog, ghost / suggestion ← reply.py
  send.py        open_window, pane_ready, deliver, reply, ask, _ensure_submitted,
                 hold_client                                             ← reply.py
  routing.py     resolve_target, ask_routed                              ← reply.py
  threads.py     conversation, conversation_for_session, log (+ attach_pictures via a hook) ← reply.py
  stream.py      the per-thread SSE watcher (§11)                        (new)
  stop.py        /session/stop (§12)                                     (new)
  drafts.py      draft_read / draft_write                                ← reply.py
  speech.py      speech_now, the /speech/ctl whitelist                   ← reply.py + canvas.py
  harnesses.py   the /harnesses routes                                   ← visual/agents.py
  abs_item.py    /item (ABS-only; deleted at the ABS exit)               ← visual/item.py
```

What stays in `visual`: the page and its static files, `/events` and
the hub, `/show`, `/img`, `/persona`, `/seen`, `GET /pair` (the page's
amux-token installer), the video poller, the image generators and engines.
`speech_state()` stays in `visual` as the producer of the `state` frame. The
server's `speech.py` calls it through a function passed in at startup, so
the server never imports `visual`.

`attach_pictures` needs the visual spool, which the server should not know
about. The server takes a `pictures_for(key) -> (images, figure)` callback,
and `visual` registers it at startup.

## One process, one port, for now

Clients know one address, and changing it is a migration nobody needs. So
`media-visual-canvas` keeps serving 8781. Its handler gains a fallthrough:
the canvas's own routes first, then `agent_media_server.app.dispatch`. A
standalone `media-server` entry point serves only the API on a port of its
own, for a host with no screens and for the hosted tier. That entry point
is where the Cloudflare link would point.

## Order (each step is its own commit, the contract test green after each)

1. Create the package with `panes.py` and `auth_abs.py`. `visual` imports
   them back under the old names (`canvas._classify_agent = panes.classify`,
   etc.), so nothing else changes yet. This removes the two-way dependency.
2. Move `reply.py` into `sessions`, `send`, `routing`, `threads` and
   `drafts`, leaving `agent_media_visual.reply` as a re-export shim. The
   tests monkeypatch `reply.*`, so they move alongside and patch the new
   module paths. A monkeypatch on the shim does not reach the real module;
   that is the trap in this step.
3. Move the route table: `app.py` owns every app route in §6, and
   `canvas.Handler` falls through to it. `test_contract.py` moves to
   `packages/server/tests/`, unchanged except for the import. It passing
   unchanged is the proof the move changed no behaviour.
4. Move `agents.py` and `item.py`. Delete the shims once nothing imports
   them (`git grep agent_media_visual.reply`).
5. Delete the dead routes (`GET /sessions`, `GET /seen`, `POST /ctl`,
   `/play`, `/say`) after one more usage check. Fix the stale comments,
   the Caddyfile and `bg.js`.
6. Only then build v1 (§9–§13), in the new package.

## What this is not

- Not a port or protocol change: steps 1–5 change no route, no shape and no
  status. The contract test is the guarantee.
- Not a rename of `visual`. It stays the canvas, which is what it is.
- Not the ABS exit. `auth_abs.py` and `abs_item.py` are isolated precisely
  so that exit is a deletion.

## Risks

- **Parallel sessions share the working tree.** Step 2 touches a 2,300-line
  file that other sessions edit often. Do it in a worktree, in one sitting,
  and rebase rather than merge.
- **Deploy.** The canvas runs on red5 *and* p8a, from their own checkouts.
  A new package means `pip install -e packages/server` on both, or p8a's
  canvas fails at import. Add the package to `media-setup` and to
  `media doctor`'s dead-install check in step 1, not later.
- **Monkeypatch drift** (step 2, above). The contract test catches the
  shape side; `test_reply.py` catches the rest only if its patches follow
  the code.
