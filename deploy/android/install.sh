#!/data/data/com.termux/files/usr/bin/bash
# Sasonica's server on this Android device (phone, tablet, TV box, Chromebook):
# one line in Termux, then the app pairs.
#
#   curl -fsSL <where this is served> | bash
#
# docs/proposals/2026-09-27-server-on-the-phone.md, step 1.
#
# Termux is only the host: the services, `am`, adb. The server and the
# agents live in a Debian proot (proot-distro), because what they are built
# for is glibc Linux: Claude Code and opencode do not run on Android's own
# libc, and the server's Python dependencies (pydantic-core, rpds-py) have
# ready wheels for glibc but need a Rust toolchain in plain Termux (596 MB,
# then a long compile). Debian with the server is ~425 MB and compiles
# nothing.
#
# What it does:
#   1. Termux: proot-distro, termux-services; Debian inside it;
#   2. Debian: Python, git, tmux, Node; agent-media cloned to
#      ~/projects/agent-media in a venv; opencode (free models, no sign-in);
#   3. this host's config: role `origin`, headless sessions on;
#   4. two Termux runit services that start the canvas (loopback :8781) and
#      the session holder inside Debian; a wake lock; Termux open to the
#      app's commands (allow-external-apps);
#   5. a pairing code, handed to the app as a sasonica://pair link.
#
# Safe to run again: it pulls, reinstalls and re-pairs, and never overwrites
# a config it finds.
#
# Settings (environment):
#   AGENT_MEDIA_REPO  git URL (default: the public repo)
#   AGENT_MEDIA_REF   branch (default: main)
#   AGENT_MEDIA_SRC   a tarball of a checkout to install instead (tests)
#   SASONICA_DISTRO   the proot-distro alias (default: debian)
#   SASONICA_DEVICE   the name the app is paired as (default: the device's
#                     model, as Android names it, e.g. "Pixel 8a")
#   SASONICA_AGENTS   agents to install in Debian (default: opencode;
#                     also: claude)
set -euo pipefail

REPO=${AGENT_MEDIA_REPO:-https://github.com/davidj4tech/agent-media.git}
REF=${AGENT_MEDIA_REF:-main}
DISTRO=${SASONICA_DISTRO:-debian}
DEVICE=${SASONICA_DEVICE:-$(getprop ro.product.model 2>/dev/null || true)}
DEVICE=${DEVICE:-This device}
AGENTS=${SASONICA_AGENTS:-opencode}
PORT=8781
SV=$PREFIX/var/service

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\ninstall: %s\n' "$*" >&2; exit 1; }
# A command inside Debian, as its root user (proot's root: this app's uid).
in_debian() { proot-distro login "$DISTRO" --shared-tmp -- bash -lc "$1"; }

case "${PREFIX:-}" in
  /data/data/com.termux/*) ;;
  *) die "run this in Termux (PREFIX is '${PREFIX:-unset}')." ;;
esac

step "Termux packages"
yes | pkg update >/dev/null 2>&1 || true
pkg install -y proot-distro termux-services termux-tools curl >/dev/null

step "Debian (proot)"
if [ ! -d "$PREFIX/var/lib/proot-distro/installed-rootfs/$DISTRO" ]; then
  proot-distro install "$DISTRO" >/dev/null
fi
in_debian 'export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null
  apt-get install -y -qq python3-venv git curl unzip tmux nodejs npm ca-certificates >/dev/null'

step "agent-media"
if [ -n "${AGENT_MEDIA_SRC:-}" ]; then
  cp "$AGENT_MEDIA_SRC" "$PREFIX/tmp/agent-media.tgz"
  in_debian 'rm -rf ~/projects/agent-media && mkdir -p ~/projects/agent-media &&
    tar -xzf /tmp/agent-media.tgz -C ~/projects/agent-media'
else
  in_debian "if [ -d ~/projects/agent-media/.git ]; then git -C ~/projects/agent-media pull --ff-only -q
    else mkdir -p ~/projects && git clone -q --depth 1 --branch '$REF' '$REPO' ~/projects/agent-media; fi"
fi
in_debian 'cd ~/projects/agent-media
  [ -x .venv/bin/python ] || python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -e packages/core -e packages/server -e packages/visual'

step "Agents: $AGENTS"
for agent in $AGENTS; do
  case $agent in
    opencode) in_debian 'command -v opencode >/dev/null || [ -x ~/.opencode/bin/opencode ] ||
                curl -fsSL https://opencode.ai/install | bash >/dev/null' ;;
    claude)   in_debian 'command -v claude >/dev/null || npm install -g -q @anthropic-ai/claude-code >/dev/null' ;;
    *) echo "  unknown agent '$agent', skipped" ;;
  esac
done
# How the server hears the agents' replies: opencode loads every script in
# its plugins folder; Claude Code takes hooks in settings.json. (Not
# `media-setup profile`: inside the proot it would also try to install
# services, which Termux's runit holds here.)
in_debian 'export PATH=~/projects/agent-media/.venv/bin:$PATH
  mkdir -p ~/.config/opencode/plugins
  ln -sf ~/projects/agent-media/packages/core/opencode/agent-media.js ~/.config/opencode/plugins/agent-media.js
  # opencode installs a plugin'"'"'s dependencies on its first start (~1 min in
  # a proot), past the server'"'"'s 45 s wait for a new chat'"'"'s pane: do it now.
  # `debug config` loads the plugins without asking any model.
  if [ -x ~/.opencode/bin/opencode ]; then ~/.opencode/bin/opencode debug config >/dev/null 2>&1 || true; fi
  if command -v claude >/dev/null; then media-setup install-hooks >/dev/null; fi'

step "This device's config"
in_debian 'export PATH=~/projects/agent-media/.venv/bin:$PATH
  [ -f ~/.config/agent-media/config.toml ] || media-setup init --roles origin >/dev/null
  touch ~/.config/agent-media.env
  grep -q "^MEDIA_HEADLESS=" ~/.config/agent-media.env || echo MEDIA_HEADLESS=1 >>~/.config/agent-media.env'
mkdir -p "$HOME/.termux"
touch "$HOME/.termux/termux.properties"
grep -q '^allow-external-apps' "$HOME/.termux/termux.properties" ||
  echo 'allow-external-apps = true' >>"$HOME/.termux/termux.properties"
command -v termux-reload-settings >/dev/null && termux-reload-settings 2>/dev/null || true

step "Services"
# Termux's runit starts each one inside Debian. The same PATH for both, so the
# session holder finds the agents opencode's and npm's installers put there.
service() { # name, command inside Debian
  mkdir -p "$SV/$1/log"
  cat >"$SV/$1/run" <<EOF
#!$PREFIX/bin/sh
# Written by deploy/android/install.sh (agent-media): Sasonica's server on this device.
exec 2>&1
exec proot-distro login $DISTRO --shared-tmp -- bash -lc '
  export PATH=~/projects/agent-media/.venv/bin:~/.opencode/bin:\$PATH
  [ -f ~/.config/agent-media.env ] && set -a && . ~/.config/agent-media.env && set +a
  exec $2'
EOF
  cat >"$SV/$1/log/run" <<EOF
#!$PREFIX/bin/sh
mkdir -p "\$HOME/.local/state/sv-log/$1"
exec svlogd -tt "\$HOME/.local/state/sv-log/$1"
EOF
  chmod +x "$SV/$1/run" "$SV/$1/log/run"
}
service sasonica-canvas "media-visual-canvas --bind 127.0.0.1 --port $PORT"
service sasonica-sessiond "media sessiond"
# termux-services starts runsvdir from a login shell; a first install has
# not had one since the package arrived.
if ! pgrep -x runsvdir >/dev/null; then
  # shellcheck disable=SC1091
  . "$PREFIX/etc/profile.d/start-services.sh" 2>/dev/null || true
  sleep 3
fi
for s in sasonica-canvas sasonica-sessiond; do sv up "$s" 2>/dev/null || true; done
command -v termux-wake-lock >/dev/null && termux-wake-lock 2>/dev/null || true

step "Waiting for the server"
up=
for _ in $(seq 1 45); do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then up=1; break; fi
  sleep 1
done
[ -n "$up" ] || die "the server did not answer on 127.0.0.1:$PORT — see ~/.local/state/sv-log/sasonica-canvas/current"

step "Pairing"
link=$(in_debian "~/projects/agent-media/.venv/bin/media-visual-canvas pair --device '$DEVICE' --host 127.0.0.1 --port $PORT" |
  grep -o 'sasonica://pair?[^[:space:]]*' | head -1)
[ -n "$link" ] || die "could not mint a pairing code."
if command -v am >/dev/null && am start -a android.intent.action.VIEW -d "$link" >/dev/null 2>&1; then
  echo "Opening Sasonica to pair. If it does not open, paste this into its pairing screen:"
else
  echo "Paste this into Sasonica's pairing screen:"
fi
echo "  $link"
echo
echo "The server runs in Termux. Keep Termux out of battery optimisation so Android leaves it running."
