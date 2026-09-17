#!/usr/bin/env bash
# Sasonica: put the CI build in place of a local one.
#
# The client is built by GitHub Actions (sasonica-web, workflow "Sasonica
# client") and uploaded as the `sasonica-client` artifact. This fetches the
# newest successful one and unpacks it over the checkout the Audiobookshelf
# container mounts, so red5 never has to run `pnpm build` — which is the slow
# part, and the part that wants a toolchain on a host that would rather not
# have one.
#
# Usage:  deploy/pull-client-build.sh [run-id]
#         (no argument = the newest successful run on the `sasonica` branch)
#
# Needs `gh` authenticated. Restarts Audiobookshelf at the end, because Next
# reads .next at startup.
set -euo pipefail

REPO="davidj4tech/sasonica-web"
CHECKOUT="${SASONICA_WEB:-$HOME/projects/sasonica-web}"
BRANCH="sasonica"

[ -d "$CHECKOUT" ] || { echo "no checkout at $CHECKOUT" >&2; exit 1; }

run_id="${1:-}"
if [ -z "$run_id" ]; then
  run_id=$(gh run list --repo "$REPO" --branch "$BRANCH" \
    --workflow "Sasonica client" --status success --limit 1 \
    --json databaseId --jq '.[0].databaseId')
  [ -n "$run_id" ] || { echo "no successful run to pull" >&2; exit 1; }
fi

# What that build was, so a deploy can be traced back to a commit.
gh run view "$run_id" --repo "$REPO" \
  --json headSha,displayTitle,createdAt \
  --jq '"run \(.createdAt)  \(.headSha[0:9])  \(.displayTitle)"'

staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
gh run download "$run_id" --repo "$REPO" --name sasonica-client --dir "$staging"

[ -f "$staging/.next/BUILD_ID" ] || { echo "artifact has no .next/BUILD_ID — wrong artifact?" >&2; exit 1; }

# Replace rather than merge: a stale chunk left behind by a merge is the kind
# of fault that only shows up in one route, hours later.
rm -rf "$CHECKOUT/.next"
mv "$staging/.next" "$CHECKOUT/.next"
# public/ carries the vendored pdfjs and unrar that `pnpm install` generates;
# without it a checkout that never ran an install is missing them.
rsync -a --delete "$staging/public/" "$CHECKOUT/public/"

echo "unpacked into $CHECKOUT (BUILD_ID $(cat "$CHECKOUT/.next/BUILD_ID"))"
systemctl --user restart audiobookshelf.service
echo "audiobookshelf restarted"
