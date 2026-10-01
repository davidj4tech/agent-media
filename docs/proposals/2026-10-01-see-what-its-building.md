# Proposal: see what it's building, live in the thread (1 Oct 2026)

Status: **proposed, nothing built; tailnet only, decided 1 Oct 2026** — no
wildcard address off the tailnet (David picked it from a list of new
directions, 1 Oct 2026). Adds a `previews` event to the per-thread stream
(§11), `POST /preview/open`, and a small authenticated proxy in the server;
an inline card and a native preview panel in Sasonica.

## What prompted it

An agent in a thread starts `npm run dev` or writes an `index.html`, and
says "it's running at http://localhost:5173". On the phone that address
means nothing: localhost is the phone. To try what is being built, David
has to get to a desk. The thread should show it — a card that opens the
running app in a panel, from the chat, over red5's tailnet.

## What exists

- **Each session's process is already found.** The 3 s sweep keeps
  `{session: pid}` for pane sessions (`sessions.py:201-215`, `_PIDS`) and
  sessiond gives headless ones theirs (`sessions.py:948`).
  `procmem.tree_mem_mb()` (`procmem.py:62`) already walks every
  descendant of that pid in one pass over `/proc` — a dev server started
  by the agent's Bash is one of them.
- **Nothing reads sockets.** No code in `packages/` touches `/proc/net/tcp`
  or `/proc/<pid>/fd`. `procinfo.py` (`:39` processes, `:150` cwd) is the
  cross-platform place to add it; on a Mac it would be `lsof -iTCP -sTCP:LISTEN`.
- **Tool output reaches the app, cut short.** A step carries
  `result_summary`, at most 300 chars (`transcript.py:89`, `:207`). Vite's
  "Local: http://localhost:5173/" usually fits; a backgrounded Bash returns
  only "running in background", so output alone is not enough.
- **The thread stream** (§11, `thread_events.py`) already pushes named
  events (`working`, `approval`, `agents`, …) on its 1 s / 3 s re-read. A
  new event fits beside `agents`.
- **The server is stdlib `http.server`**, no proxy code anywhere
  (`app.py:167`; the canvas is `ThreadingHTTPServer`, `canvas.py:2001`),
  bound to red5's tailnet IP (`MEDIA_VISUAL_BIND`, `canvas.py:24`).
- **Credentials where headers cannot go:** `?access_token=` already stands
  in for the bearer on the SSE routes (`app.py:695-731`). `GET /upload`
  (§6.18) serves files only under one root, symlinks resolved.
- **The canvas origin holds a secret.** `GET /pair?c=` puts the amux token
  in the canvas page's `localStorage` (`canvas.py:441-444`), and that token
  types into panes (§19). A preview served from `red5:8781` would share that
  origin, so agent-written page code could read it. Previews need their own
  origin.
- **Sasonica Shell does not expose ports.** Its "named URLs" are MCP
  connector paths (`/<secret>/<name>/mcp`) and OAuth is for the connector
  (`sasonica-shell/docs/tools-and-approvals.md:287-369`). The Cloudflare
  link (`docs/umbrella.md:19`) publishes only the server's port.
- **In the app:** tool steps render as `ToolStep` (`parts.tsx:307`),
  `[[visual:]]` figures as `PictureView` thumbnails (`parts.tsx:381`), wired
  in `Thread.tsx:78-88` (`tools.by_name`, `data.by_name`). Links go out with
  `openInBrowser` (`app/lib/native.ts:240`, `AssistPlugin.java:97`). There
  is no iframe or second WebView. The app's origin is `http://localhost`
  (`capacitor.config.ts`, `androidScheme: 'http'`), and cleartext is
  allowed (§19 C), so plain http to `red5` is not blocked.
- **The browser preview of the app** on `:8795` (`sasonica-chat-preview`,
  `serve.mjs --host 100.103.43.93`) is the same pattern by hand: bind the
  tailnet IP, open it on the phone.

## The shape

[[visual: left: a phone showing a Sasonica thread; under the agent's step "Start the dev server" an inline card "▶ Preview · Vite · :5173 · live" with buttons "Open" and "Browser". Arrow from "Open" to a full-screen panel "red5:8801 — the app", with a top bar "↻ Reload · ⧉ Browser · ✕". Right: red5 box containing "agent session (pid)" → child "vite :5173 (127.0.0.1)"; "server sweep" reads /proc and finds the listener; "preview proxy :8801 (tailnet IP)" checks a cookie and forwards to 127.0.0.1:5173. Arrow from the phone panel to ":8801" labelled "tailnet + preview cookie"; a dashed arrow "POST /preview/open (device token) → one-time link" from the card to the server.]]

### 1. Find it (server)

In the existing 3 s sweep, for each live session: read the LISTEN sockets
in `/proc/net/tcp` and `tcp6` once, map socket inodes to pids through
`/proc/<pid>/fd`, and keep those owned by the session's tree. Skip the
agent's own pid and known machinery (MCP servers, the LSP, sessiond), and
anything that does not answer `GET /` with HTTP inside 1 s. What is left is
a **preview**: `{id, port, kind: "dev", title, path, since, bound}`.
`title` is the process name (`vite`, `next`, `python -m http.server`);
`bound` says whether it listens on loopback only or on all addresses.

Tool output only *names* it: a `http://localhost:<port>/<path>` in a step's
`result_summary` or the agent's words sets `path` and confirms it sooner.
An address in words with no listener behind it is not a preview.

### 2. Files too

A Write or Edit that makes a `.html` file inside the session's `cwd` is a
preview of `kind: "file"`, served statically from that file's folder, so
its relative CSS and scripts load. Only that folder and below; no
directory listing; paths resolved like `/upload` (§6.18).

### 3. Show it on the stream

The snapshot gains `previews: [...]`, and a new `previews` event carries the
whole list when it changes (a server starts, stops, or changes port). A
preview that goes away stays one more frame as `gone: true`, so the card
can say "stopped" instead of vanishing.

### 4. Reach it (the proxy)

Each preview gets its own port on red5's tailnet IP from a small range
(`MEDIA_PREVIEW_PORTS`, default 8800–8815): its own origin, so its code
cannot read the canvas's token. The listener:

- **Checks a cookie on every request.** `POST /preview/open {session, id}`
  (device token, gated like `/reply`) answers `{url}`: a one-time link
  `http://red5:8801/__sasonica/enter?t=<ticket>` (single use, 60 s). It
  sets `sasonica_pv_<id>`, HttpOnly, SameSite=Lax, and redirects to `path`.
  Without the cookie: 403, a plain page saying "Open this from the thread".
- **Forwards** to `127.0.0.1:<port>` with `Host: localhost:<port>` (Vite and
  Next refuse unknown hosts), and splices bytes after an `Upgrade`, so hot
  reload over WebSocket keeps working.
- **Ends with the preview.** Socket gone or session ended: the port closes
  and the cookie's secret is dropped.

The proxy, not "bind your dev server to 0.0.0.0", because the agent's
server is loopback by default and should stay so, and a tailnet-wide open
port has no token at all. A dev server already on all addresses still goes
through the proxy; the card never offers its bare address.

Off the tailnet (§19 A/B) this does not work in v1: `app.ryer.org` proxies
one port. See the open questions.

### 5. The card and the panel (app)

- **Card:** a `data-preview` part after the step that started it (or at
  the end of the turn), drawn by a new `PreviewCard`: kind, title, port,
  "live" / "stopped", and **Open** and **Browser**. Stopped: greyed, no
  buttons. Registered in `Thread.tsx` under `data.by_name`.
- **Panel:** **Open** calls `/preview/open` and hands the link to a new
  native `PreviewPlugin`: a full-screen second WebView in its own activity,
  with a bar for Reload, Back, Open in browser and Close. A native WebView,
  not an iframe: the app is `http://localhost`, so an iframe's cookie is a
  third-party cookie, which Android's WebView refuses by default.
- **Browser:** the same link through `openInBrowser`; Chrome on the phone
  gets its own cookie.
- **The web build** (`:8795`) opens the link in a new tab.
- A What's new line: "When an agent starts the app it's building, open it
  from the thread."

## Order

1. Server: socket reading in `procinfo` (Linux; Mac via `lsof`), preview
   detection in the sweep, `previews` on the snapshot and stream; tests
   with a fake `/proc` like `procmem`'s. Contract §11.
2. Server: the proxy listener, tickets, cookies, WebSocket splice,
   `POST /preview/open`; tests against a throwaway local HTTP server.
   Contract: a new §6.21.
3. App: `PreviewCard`, `openInBrowser` path first (works with no native
   change), then `PreviewPlugin`; the What's new line.
4. File previews (`kind: "file"`).
5. Later, if wanted: off-tailnet reach (open question 1).

## Open questions for David

1. **Off the tailnet.** v1 is tailnet only. Later: a wildcard vhost
   (`*.pv.app.ryer.org` → one proxy port, routed by host name, with a
   wildcard cert), or none at all. Worth doing, or tailnet-only for good?
2. **Panel or browser by default** for **Open**: the in-app panel
   (recommended: stays in the chat, Back returns to the thread) or Chrome?
3. **Who may open it.** Any paired device of the owner (recommended, like
   every other route), or only the device that is in the thread?
4. **Files beyond HTML:** should a written `.svg`, `.png` or `.pdf` get a
   card too, or only pages?
5. **Tell the agent?** A hook line ("previews show on David's phone; keep
   the dev server on loopback") so agents stop adding `--host 0.0.0.0`.

## Verification

- `pytest packages/server/tests/test_previews.py`: a fake `/proc` with a
  session tree, a listener in it and one outside it → one preview; the
  agent's own pid and an MCP server's port skipped; the socket going →
  `gone: true`, then nothing.
- Proxy tests: no cookie → 403; a used or old ticket → 403; a good ticket
  → cookie, redirect, the upstream page; `Host` rewritten; a WebSocket
  echo goes through.
- On red5 with p8a: a headless thread runs `npm create vite` and `npm run
  dev` → the card appears within ~3 s; **Open** shows the page; the agent
  edits a file → the panel hot-reloads. Stop the server → "stopped" and
  the port refuses.
- From a tailnet machine with no cookie, `curl http://red5:8801/` → 403.
  `curl http://red5:5173/` → refused (still loopback).
- The canvas's amux token is unreadable from the panel: `localStorage` on
  `red5:8801` is empty.
