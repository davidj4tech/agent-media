# Proposal: one alert path for the watchers and digests, shown in Sasonica Next (24 Sep 2026)

Status: **proposal, nothing built.** Adds an alert store and routes to the
server (a new §6.x in `server-contract.md`), one event type to
`GET /sessions/events` (§6.13), a producer helper in `agent-config`, and an
Alerts view in Next.

## What prompted it

David (24 Sep 2026), after the landscape watch landed: integrate
describe-digest and the other alerts into the app, "or maybe even rewrite them
to be a bit more systemic and Next friendly".

## What exists

About a dozen timers on red5 watch something and tell David about it. Each one
does its own state keeping, deduping and delivery:

| Watcher | Lives in | Schedule | Delivers via |
|---|---|---|---|
| disk-watch | dotfiles | 15 min | digest-pane + inbox TODO; own hysteresis (90/95/98 %) |
| host-watch | dotfiles | 15 min | digest-pane + inbox TODO; own state per host |
| mcp-watch | dotfiles | boot + interval | digest-pane + inbox TODO |
| login-watch | agent-gateway | boot + interval | digest-pane + inbox TODO (752 lines) |
| cc-voice-issues-watch | dotfiles | daily 09:05 | digest-pane + inbox TODO |
| cc-issue-71749-watch | dotfiles | boot + interval | none visible (journal) |
| pr264-watch, discourse6289-watch | dotfiles | daily / 6 h | `notify` |
| domain-transfer-watch | ~/.local/bin (no repo) | boot + interval | `media say` |
| agent-memory-healthcheck | agent-memory | hourly | journal only |
| describe-digest | dotfiles | daily 09:00 | its own tmux session + spoken ping |
| agent-org-agenda-digest | agent-config | daily | digest-pane |
| landscape-watch | agent-media | Mon 07:30 | commit + inbox TODO |

`agent-digest-pane` was already the first attempt at a shared path. It solves
the attention question for the desk (a tmux client active → speak; away →
ping the phone). But it delivers into tmux, which the phone app can't show.
The inbox TODO is the one thing that reliably lasts.

What goes wrong today:

- **Every watcher reimplements state.** Hysteresis, "alert once, re-arm on
  recovery" and "only while no open TODO with this heading" are copied into
  each script, slightly differently.
- **Delivery depends on which script you read.** Some speak, some pop a pane,
  some only log. agent-memory-healthcheck can fail for a week and nothing
  says so.
- **Nothing reaches Next.** The phone learns about a full disk only as a
  spoken ping with no card to go back to.
- **No clear.** When a host comes back, the TODO stays open until someone
  notices.

## The shape

[[visual: left column of producer boxes (disk, host, login, mcp, memory health, issue watches, digests) each with an arrow labelled "agent-alert report" into a central box "agent-media server: alert store (edge detection, dedupe, history)"; from it three arrows out: "GET /sessions/events → Next notification", "GET /alerts → Alerts view", "inbox.org TODO (record)"; a dashed arrow from the store to "digest-pane (fallback when server is down)"]]

### 1. Producers report status, not events

A watcher's only job becomes: *check, then report what is true now*.

```
agent-alert report disk.red5.root --level warn \
  --title "red5 root is 93% full" \
  --detail "$(df -h / | tail -1)" \
  --fix "Free space on red5: see the disk cleanup TODO"
agent-alert report disk.red5.root --level ok      # every run while fine
```

- `id` is stable per thing watched (`disk.<host>.<mount>`,
  `host.<name>`, `login.chatgpt`, `memory.health`).
- `level`: `ok` | `info` | `warn` | `needs` (needs David to act).
- `kind`: `status` (default: a condition that raises and clears) or `digest`
  (a one-off report with a markdown body: describe-digest, agenda, landscape).
- Reporting `ok` every run is what makes clearing automatic. The producer
  keeps **no state file**, and the hysteresis moves to the store.

`agent-alert` lives in **agent-config** (neutral tooling, per AGENTS.md). It
POSTs to the local server. If the server is unreachable it falls back to what
happens today (digest-pane + inbox TODO), so a dead server never silences a
watcher. The one watcher that watches the server itself needs that fallback
most.

### 2. The server owns state and delivery

A small SQLite table (`alerts`: id, level, kind, title, detail, fix, first_seen,
last_seen, cleared_at, acked_at) behind:

- `POST /alerts` — report (loopback or a device token with a new
  `may_report` scope; producers on other hosts, such as the phone's
  call-guard health, use the token).
- `GET /alerts[?open=1]` — the list, newest first.
- `POST /alerts/{id}/ack` — "seen it", which stops re-notifying but does not
  clear.
- `GET /sessions/events` gains an **`alerts` event**, sent when a row changes
  level (additive: today's clients ignore unknown events). Next's
  `NotifyService` already holds this connection, so no second socket and no
  extra battery.

Edge detection is in one place. Only a *change* of level notifies: ok→warn,
warn→needs, needs→ok (a quiet "cleared" line, no buzz). Re-reporting the same
level only bumps `last_seen`. A `status` alert that stops being reported
entirely (its producer died) turns into its own warning after 3× its usual
interval. That is the gap that hid agent-memory-healthcheck.

### 3. What David sees

- **Notification** for `needs` always and `warn` by default (a Settings toggle
  per level), grouped as one Android channel, "Alerts".
- **Alerts view in Next** (a Home section, or its own tab if it earns one):
  open alerts first, then recent digests as cards, then cleared history.
  Each card has **Fix it**, which starts a new session through the existing
  `POST /ask` with the alert's title, detail and fix as the prompt, so an
  agent investigates. **Ack** is the second button.
- **Speech** only for `needs`, and only at the level the current speech policy
  allows. Digests are no longer spoken by default; they wait as cards. This
  retires describe-digest's own tmux session.
- **inbox.org** keeps the record. The first raise to `warn`/`needs` files a TODO
  with an `:ALERT_ID:` property (dedupe by property, not by heading text), and a
  clear appends `Cleared <timestamp>` under it. Whether a clear also marks it
  DONE is open question 1.

## Migration

In order, each step useful on its own:

1. **Store + routes + `agent-alert` with fallback.** Port **disk-watch** and
   **host-watch** first. They are small, their state files show exactly what
   the store must reproduce, and they alert rarely, so a mistake is cheap.
2. **`alerts` event + Next notification + a minimal list.** Test that it
   survives Doze and a reboot, the same untested cases as §6.13's notifier.
3. **Digests as cards.** describe-digest, agenda digest, landscape watch.
   The landscape watch stops filing its own TODO.
4. **The rest.** login-watch (its producer stays in agent-gateway; only the
   delivery tail changes), mcp-watch, memory healthcheck, the issue/PR
   watches, domain-transfer-watch. This also gives the homeless units an
   owner, which closes the inbox TODO about them.
5. **Retire** `agent-digest-pane` as a delivery path once nothing but the
   fallback uses it.

## Open questions

1. **Does a clear close the org TODO?** Recommend: yes for `status` alerts that
   cleared on their own (host back, disk freed), with a `Cleared` note; leave it
   open if David acked it or it was `needs`.
2. **Where do alerts live in Next: a Home section, or a tab?** Recommend: a Home
   section with a count badge; promote it to a tab only if it gets busy.
3. **Hosts other than red5** (p8a, sp4, pn) report to red5's server over the
   tailnet with a device token. Or does each host run its own store? Recommend:
   red5 is the one store; the fallback covers red5 being down.
