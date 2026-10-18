# Agent notes

agent-media is the agent's audio/music stack: it gives coding agents a voice,
whole-house playback control and music awareness, on any Linux box or Termux.
See README.md for the packages and docs/server-contract.md for the server.

## Restarting services

- Restart with `media restart-services --only <name> --after 10`, e.g.
  `media restart-services --only sessiond --after 10` (short names: `sessiond`,
  `canvas`, `tts-shim`). It refuses while the service is busy (a session
  working, a reply being spoken), runs detached so it outlives your turn,
  re-checks before acting, health-checks each unit and raises the
  `restart-services.<host>` alert on failure. Add `--dry-run` to see the busy
  checks. Details: docs/reference/version-skew-monitoring.md ("Restarting
  safely, and from inside a chat") and
  packages/core/src/agent_media_core/restart_services.py.
- Never `systemctl restart agent-media-sessiond` (or any direct restart of
  sessiond) from inside a chat: a phone-started chat is a sessiond child and
  is killed mid-turn.
- A canvas page-only change (static/canvas.{html,css,js}, view.html) needs no
  restart: the page is re-read when its files change. A Python change does.
- `--force` skips the busy checks: use it only when David asks.

## Git

- No `Co-Authored-By` trailers or other AI-attribution lines in commits.
- Parallel sessions share this working tree: stage by path (check
  `git diff --cached`), never `git add -A`.
