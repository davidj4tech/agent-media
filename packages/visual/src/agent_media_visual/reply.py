"""Moved: the reply half of the canvas is now the server package.

What was here — ABS identity, session discovery, sending into panes, the
conversation threads, drafts and the speech bar's naming — lives in
`agent_media_server` (auth_abs, sessions, send, routing, threads, drafts,
speech, share); see docs/proposals/2026-09-21-server-package.md.

This module re-exports every name it used to define so old imports and
callers still resolve. It is a copy of the names, not the modules: a
monkeypatch here reaches nothing, because the moved code looks its
collaborators up in its own module. Patch the real module
(`agent_media_server.sessions.live_sessions`, not `reply.live_sessions`).
Delete this once nothing imports it (`git grep agent_media_visual.reply`).
"""

from agent_media_server import (auth_abs, drafts, panes, routing, send, sessions, share,
                                speech, threads)

from . import state as _state

for _mod in (auth_abs, sessions, send, routing, threads, speech, drafts, share):
    globals().update({k: v for k, v in vars(_mod).items()
                      if not k.startswith("__") and k not in ("annotations",)})
_tmux = panes._tmux
del _mod

# The log's pictures come from the canvas's spool, through a callback the
# server takes rather than an import (see threads.set_pictures_for). The
# canvas registers it at import too; doing it here as well keeps an old
# caller of `reply.log_for_item` getting its pictures.
threads.set_pictures_for(_state.pictures_for)
