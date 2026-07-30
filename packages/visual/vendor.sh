#!/usr/bin/env bash
# Vendor the front-end libs the canvas serves at /vendor/<f>: an established
# markdown renderer (marked) + sanitizer (DOMPurify). Downloaded ONCE to disk,
# not a runtime CDN dependency — the canvas serves them locally so the page
# stays self-contained and works offline / on the tailnet / e-ink.
#
# Re-run to update. If you skip this, #peek still works — it degrades to
# escaped text (and the visual-cue chips still render).
set -euo pipefail

DEST="$(cd "$(dirname "$0")" && pwd)/src/agent_media_visual/vendor"
mkdir -p "$DEST"

# Pinned versions (bump deliberately; re-check the sanitizer's release notes).
MARKED_VER="12.0.2"
PURIFY_VER="3.1.6"

fetch() {  # url dest
	echo "→ $2"
	curl -fsSL "$1" -o "$2"
}

fetch "https://cdn.jsdelivr.net/npm/marked@${MARKED_VER}/marked.min.js" \
      "$DEST/marked.min.js"
fetch "https://cdn.jsdelivr.net/npm/dompurify@${PURIFY_VER}/dist/purify.min.js" \
      "$DEST/purify.min.js"

echo "vendored into $DEST"
ls -l "$DEST"
