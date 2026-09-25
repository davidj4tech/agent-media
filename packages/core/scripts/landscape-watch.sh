#!/usr/bin/env bash
#
# landscape-watch — weekly look at projects similar to agent-media / Sasonica.
#
# Runs a headless Claude (web search + gh) over docs/landscape/watchlist.md,
# writes docs/landscape/YYYY-MM-DD.md, commits ONLY that file (the tree is
# shared with live sessions), files a TODO in ~/org/inbox.org linking it, and
# reports it to the alert store as a digest whose read-out waits behind a Play.
# Wired as a weekly user timer (deploy/systemd/landscape-watch.*).
set -euo pipefail

REPO="${LANDSCAPE_REPO:-$HOME/projects/agent-media}"
DIR="docs/landscape"
TODAY="$(date +%F)"
OUT="$DIR/$TODAY.md"
INBOX="${LANDSCAPE_INBOX:-$HOME/org/inbox.org}"
MODEL="${LANDSCAPE_MODEL:-sonnet}"

cd "$REPO"
prev="$(ls "$DIR"/20??-??-??.md 2>/dev/null | grep -v "$TODAY" | sort | tail -1 || true)"

prompt="You are the weekly landscape watch for agent-media and Sasonica (a phone
chat/voice front-end for coding agents; see docs/sasonica-roadmap.md for what we
are building now).

1. Read $DIR/watchlist.md.${prev:+ The previous digest is $prev; report what is NEW since it, do not repeat it.}
2. For each entry, check recent releases, merged PRs, changelogs and notable
   issues (use \`gh\` for GitHub repos). Verify repo
   names; if one moved or died, say so.
3. Also use WebSearch (required, not optional) for each non-GitHub entry,
   Anthropic's Claude Code changelog, and discovery: search GitHub, Hacker News and the web for NEW projects in the
   same space (phone/remote UIs for Claude Code, Codex, opencode; voice or TTS
   for coding agents) from roughly the last month.
4. Write $OUT in this shape:
   # Landscape watch — $TODAY
   ## Worth stealing  (at most 5 bullets: the idea, where it is seen with a
      link, and which part of our roadmap it touches; this is the point)
   ## Per project  (one short paragraph each; 'nothing notable' is fine)
   ## Suggested additions  (new projects with a link and one line why)
   Keep it under ~120 lines. Ideas and patterns, not star counts.
Only write that one file. Do not edit anything else."

claude -p "$prompt" \
  --model "$MODEL" \
  --allowedTools "Read,Glob,Grep,WebSearch,WebFetch,Bash(gh:*),Edit($DIR/**)" \
  --permission-mode acceptEdits \
  > "${XDG_STATE_HOME:-$HOME/.local/state}/landscape-watch.last.log" 2>&1

[ -s "$OUT" ] || { echo "landscape-watch: no digest written (see landscape-watch.last.log)"; exit 1; }

git add -- "$OUT"
git commit -q -m "docs: landscape watch $TODAY" -- "$OUT" || true

# Top item of "Worth stealing" makes the TODO title informative on the agenda.
lead="$(awk '/^## Worth stealing/{f=1;next} /^## /{f=0} f && /^[-*] /{sub(/^[-*] +/,"");print;exit}' "$OUT" | sed -E 's/\*\*//g; s/\. .*/./' | cut -c1-90)"
{
  printf '\n* TODO Landscape watch %s: %s\n' "$TODAY" "${lead:-read the digest}"
  printf '  [[file:%s/%s][Digest]] — skim "Worth stealing"; move good suggested additions into the watch list.\n' "$REPO" "$OUT"
} >> "$INBOX"

# A digest in the alert store: its "Worth stealing" is the read-out, rendered
# held — played from the app's Home or `prefix y`, never on its own (David,
# 25 Sep 2026). The TODO above stays the record.
if command -v agent-alert >/dev/null 2>&1; then
  worth="$(awk '/^## Worth stealing/{f=1;next} /^## /{f=0} f' "$OUT" | sed -E 's/^[-*] +//; s/\*\*//g; s/\[([^]]*)\]\([^)]*\)/\1/g; s/<?https?:[^ )>]*>?//g' | grep -v '^[[:space:]]*$' || true)"
  agent-alert report digest.landscape --kind digest --level info \
    --title "Landscape watch $TODAY${lead:+: $lead}" \
    --detail - \
    --spoken "${worth:+Landscape watch. Worth stealing. $worth}" <"$OUT" || true
fi

echo "landscape-watch: wrote $OUT"
