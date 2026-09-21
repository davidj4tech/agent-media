"""agent-media-server — the app's API, split out of the canvas.

`visual` began as the canvas and became the app's server by accretion. This
package holds the server half: which sessions exist and how to reach their
panes, sending into them, the conversation threads, drafts, and the ABS
identity in front of all of it. `visual` imports it and serves it on the same
port; nothing here imports `visual`. What the server needs from the canvas
(its speech snapshot, its picture spool) is handed in as callbacks.

The contract it serves is docs/server-contract.md; the plan for the split is
docs/proposals/2026-09-21-server-package.md.
"""
