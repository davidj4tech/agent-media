# vendored front-end libs

Served by `canvas.py` at `/vendor/<file>` and used by `#peek` to render
transcript turns as sanitized markdown.

Populate with `packages/visual/vendor.sh` (downloads pinned `marked.min.js` +
`purify.min.js` here). Not hand-edited. If these files are absent, `#peek`
degrades to escaped plain text — the visual-cue chips still render either way.
