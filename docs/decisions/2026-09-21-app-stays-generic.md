# Decision: the app stays generic, the server knows the setup

Status: **accepted**
Date: 2026-09-21

## The rule

The Sasonica app renders what the server answers. Anything that depends on
how one host is arranged (its directories, its tmux session names, its
library names, which machine is the phone) lives on the server, behind an
endpoint, and never in the app.

When the phone needs to know something about the host, add a server
endpoint that answers it and let the app render the answer. Someone else's
server answers with their own setup and the app is unchanged.

## Why

The app is meant to be distributable (see
`docs/proposals/2026-08-20-monetization.md`). A feature that only works
because of how one desk is arranged is a bug in a product, even when it
works perfectly on that desk.

It came up on the New chat picker (David, 21 Sep 2026). Its list of
"projects" came from the Audiobookshelf series of a library that had to be
named `Conversations`, and the series names had to match the desk's
`p-<dir>` tmux session names. The feature was stopped and replaced by
`GET /targets`: the sessions plus `places`, the directories sessions have
actually run in.

## Consequences

- A new source of sessions belongs behind the server's abstraction, not in
  the client.
- A destination is offered only if the server can **list, capture and send**
  to it. tmux and herdr have all three. An editor session (VS Code, found
  through `~/.claude/ide/*.lock`) has no send path, so it stays out: a picker
  that offers a destination it cannot deliver to is worse than a shorter
  list.
- Hostnames belong in the server's config (`~/.config/agent-media/config.toml`
  peers, env), never in either codebase.

See also `docs/proposals/2026-09-21-what-a-chat-can-be-pointed-at.md`.
