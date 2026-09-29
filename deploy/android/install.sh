#!/data/data/com.termux/files/usr/bin/bash
# Sasonica's server on this Android device (phone, tablet, TV box, Chromebook):
# one line in Termux, then the app pairs.
#
#   curl -fsSL <where this is served> | bash
#
# docs/proposals/2026-09-27-server-on-the-phone.md, step 1.
#
# Termux is only the host: the services, `am`, adb (sasonica-adb). The
# server and the agents live in a Debian proot (proot-distro), because what
# they are built for is glibc Linux: Claude Code and opencode do not run on Android's own
# libc, and the server's Python dependencies (pydantic-core, rpds-py) have
# ready wheels for glibc but need a Rust toolchain in plain Termux (596 MB,
# then a long compile). Debian with the server is ~425 MB and compiles
# nothing.
#
# What it does:
#   1. Termux: proot-distro, termux-services; Debian inside it;
#   2. Debian: Node only when Claude Code is chosen (no tmux: the chats the
#      app starts are headless); Sasonica's server as one file (the server-latest
#      release, deploy/binary/build.sh) at ~/.local/bin/sasonica, checked
#      against its SHA256SUMS; opencode (free models, no sign-in);
#   3. this host's config: role `origin`, headless sessions on;
#   4. two Termux runit services that start the canvas (loopback :8781) and
#      the session holder inside Debian; a wake lock; Termux open to the
#      app's commands (allow-external-apps);
#   5. the Sasonica app, from sasonica.com/app, when it is not installed;
#   6. a pairing code, handed to the app as a sasonica://pair link.
#
# Safe to run again: it fetches the latest binary, reinstalls and re-pairs,
# and never overwrites a config it finds.
#
# Settings (environment):
#   SASONICA_FROM     binary (default) | source: a git checkout in a venv at
#                     ~/projects/agent-media, as before the binary (for
#                     working on the server on the phone)
#   SASONICA_BINARY_BASE  where the binary is (default: the server-latest
#                     release; tests: a file:// directory)
#   SASONICA_INSTALL_BASE where sasonica-adb is fetched from (default: main)
#   AGENT_MEDIA_REPO  git URL, source only (default: the public repo)
#   AGENT_MEDIA_REF   branch, source only (default: main)
#   AGENT_MEDIA_SRC   a tarball of a checkout to install instead (source; tests)
#   SASONICA_DISTRO   the proot-distro alias (default: debian)
#   SASONICA_DEVICE   the name the app is paired as (default: the device's
#                     model, as Android names it, e.g. "Pixel 8a")
#   SASONICA_AGENTS   agents to install in Debian (default: opencode;
#                     also: claude)
set -euo pipefail

FROM=${SASONICA_FROM:-binary}
BINARY_BASE=${SASONICA_BINARY_BASE:-https://github.com/davidj4tech/agent-media/releases/download/server-latest}
INSTALL_BASE=${SASONICA_INSTALL_BASE:-https://raw.githubusercontent.com/davidj4tech/agent-media/main/deploy}
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
case $FROM in binary | source) ;; *) die "SASONICA_FROM is binary or source, not '$FROM'." ;; esac
# Where the server's commands are inside Debian: the binary's shims, or the
# checkout's venv.
if [ "$FROM" = binary ]; then SBIN='~/.local/bin'; else SBIN='~/projects/agent-media/.venv/bin'; fi

step "Termux packages"
yes | pkg update >/dev/null 2>&1 || true
pkg install -y proot-distro termux-services termux-tools curl android-tools >/dev/null

step "Debian (proot)"
if [ ! -d "$PREFIX/var/lib/proot-distro/installed-rootfs/$DISTRO" ]; then
  proot-distro install "$DISTRO" >/dev/null
fi
in_debian 'export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null
  pkgs="curl unzip ca-certificates"
  # No tmux for the binary: Claude Code and opencode both run headless
  # (sessiond), so nothing the app starts opens a pane. A source install
  # keeps it, for working on the server at a terminal.
  [ '"$FROM"' = source ] && pkgs="$pkgs python3-venv git tmux"
  # Node only for Claude Code (npm installs it); opencode brings its own runtime.
  case " '"$AGENTS"' " in *" claude "*) pkgs="$pkgs nodejs npm" ;; esac
  apt-get install -y -qq $pkgs >/dev/null'

if [ "$FROM" = binary ]; then
step "Sasonica's server"
# Debian's own architecture (arm64 on a phone), into its ~/.local/bin; a
# rename, so a running server keeps the file it started from.
in_debian "set -e; mkdir -p ~/.local/bin
  case \$(uname -m) in aarch64 | arm64) arch=aarch64 ;; x86_64) arch=x86_64 ;;
    *) echo \"no Sasonica server for \$(uname -m)\" >&2; exit 1 ;; esac
  curl -fsSL '$BINARY_BASE/sasonica-linux-'\$arch -o ~/.local/bin/sasonica.new
  want=\$(curl -fsSL '$BINARY_BASE/SHA256SUMS' | awk -v f=sasonica-linux-\$arch '\$2 == f {print \$1}')
  got=\$(sha256sum ~/.local/bin/sasonica.new | cut -d' ' -f1)
  if [ -z \"\$want\" ] || [ \"\$got\" != \"\$want\" ]; then
    rm -f ~/.local/bin/sasonica.new; echo 'the download does not match its checksum' >&2; exit 1; fi
  chmod +x ~/.local/bin/sasonica.new && mv -f ~/.local/bin/sasonica.new ~/.local/bin/sasonica
  ~/.local/bin/sasonica version" || die "could not install Sasonica's server."
else
step "agent-media (source)"
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
fi

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
# its plugins folder; Claude Code takes hooks in settings.json. The binary's
# `sasonica install` does both, with the shims they call and this host's
# config (role `origin`, headless sessions on); its services are Termux's
# runit here, below. (Not `media-setup profile`: inside the proot it would
# also try to install services.)
if [ "$FROM" = binary ]; then
  in_debian 'export PATH=~/.local/bin:~/.opencode/bin:$PATH
    sasonica install --no-services' || die "sasonica install failed."
else
  in_debian 'export PATH=~/projects/agent-media/.venv/bin:$PATH
    mkdir -p ~/.config/opencode/plugins
    ln -sf ~/projects/agent-media/packages/core/opencode/agent-media.js ~/.config/opencode/plugins/agent-media.js
    if command -v claude >/dev/null; then media-setup install-hooks >/dev/null; fi'
fi
in_debian '# opencode installs a plugin'"'"'s dependencies on its first start (~1 min in
  # a proot), close to sessiond'"'"'s 60 s wait for its server to answer: do it now.
  # `debug config` loads the plugins without asking any model.
  if [ -x ~/.opencode/bin/opencode ]; then ~/.opencode/bin/opencode debug config >/dev/null 2>&1 || true; fi'

# The ADB power-up's helper (the app runs it through RUN_COMMAND), on
# Termux's PATH: fetched with the binary (a rerun updates it), or straight
# from the checkout, so a pull updates it.
ROOTFS=$PREFIX/var/lib/proot-distro/installed-rootfs/$DISTRO
if [ "$FROM" = binary ]; then
  rm -f "$PREFIX/bin/sasonica-adb"
  curl -fsSL "$INSTALL_BASE/android/sasonica-adb" -o "$PREFIX/bin/sasonica-adb" &&
    chmod +x "$PREFIX/bin/sasonica-adb" || echo "  (sasonica-adb not fetched: the ADB power-up will say so)"
else
  ln -sf "$ROOTFS/root/projects/agent-media/deploy/android/sasonica-adb" "$PREFIX/bin/sasonica-adb"
fi

step "This device's config"
[ "$FROM" = binary ] || in_debian 'export PATH=~/projects/agent-media/.venv/bin:$PATH
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
  export PATH=$SBIN:~/.opencode/bin:\$PATH
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
if [ "$FROM" = binary ]; then
  service sasonica-canvas "sasonica serve --bind 127.0.0.1 --port $PORT"
  service sasonica-sessiond "sasonica sessiond"
else
  service sasonica-canvas "media-visual-canvas --bind 127.0.0.1 --port $PORT"
  service sasonica-sessiond "media sessiond"
fi
# termux-services starts runsvdir from a login shell; a first install has
# not had one since the package arrived.
if ! pgrep -x runsvdir >/dev/null; then
  # shellcheck disable=SC1091
  . "$PREFIX/etc/profile.d/start-services.sh" 2>/dev/null || true
  sleep 3
fi
for s in sasonica-canvas sasonica-sessiond; do sv up "$s" 2>/dev/null || true; done
command -v termux-wake-lock >/dev/null && termux-wake-lock 2>/dev/null || true
# After a restart nothing starts Termux's services until Termux itself is
# opened. Termux:Boot (a separate app, from the same place as Termux) runs
# ~/.termux/boot/ at boot; this is what it runs. The app's "Keep it running"
# says whether Termux:Boot is there.
mkdir -p "$HOME/.termux/boot"
cat >"$HOME/.termux/boot/sasonica" <<EOF
#!$PREFIX/bin/sh
# Written by deploy/android/install.sh (agent-media): the server after a restart.
termux-wake-lock
. $PREFIX/etc/profile.d/start-services.sh
EOF
chmod +x "$HOME/.termux/boot/sasonica"

step "Waiting for the server"
up=
for _ in $(seq 1 45); do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then up=1; break; fi
  sleep 1
done
[ -n "$up" ] || die "the server did not answer on 127.0.0.1:$PORT — see ~/.local/state/sv-log/sasonica-canvas/current"

step "The Sasonica app"
# Someone who found sasonica.com/install before the app: fetch it from
# sasonica.com/app (checked against its version.json) and open Android's own
# install screen. Android asks once to allow installs from Termux.
PM=${SASONICA_PM:-/system/bin/pm}  # tests: a stand-in
has_app() { "$PM" path com.sasonica.app >/dev/null 2>&1; }
if [ ! -x "$PM" ]; then
  echo "  (not on Android: skipped)"
elif has_app; then
  echo "  installed"
else
  apk=$HOME/Sasonica.apk
  want=$(curl -fsSL -m 20 https://sasonica.com/app/version.json | sed -n 's/.*"sha256": *"\([0-9a-f]*\)".*/\1/p' || true)
  if curl -fsSL -m 300 https://sasonica.com/app -o "$apk" &&
     [ -n "$want" ] && [ "$(sha256sum "$apk" | cut -d' ' -f1)" = "$want" ]; then
    echo "  Tap Install when Android asks. (The first time, allow Termux to install apps.)"
    termux-open "$apk" 2>/dev/null || true
    for _ in $(seq 1 60); do has_app && break; sleep 3; done
    if has_app; then echo "  installed"; rm -f "$apk"; else echo "  not installed yet: it is at $apk, or https://sasonica.com/app"; fi
  else
    rm -f "$apk"
    echo "  could not fetch it: get it from https://sasonica.com/app"
  fi
fi

step "Pairing"
link=$(in_debian "$SBIN/media-visual-canvas pair --device '$DEVICE' --host 127.0.0.1 --port $PORT" |
  grep -o 'sasonica://pair?[^[:space:]]*' | head -1)
[ -n "$link" ] || die "could not mint a pairing code."
if command -v am >/dev/null && am start -a android.intent.action.VIEW -d "$link" >/dev/null 2>&1; then
  echo "Opening Sasonica to pair. If it does not open, paste this into its pairing screen:"
else
  echo "Paste this into Sasonica's pairing screen:"
fi
echo "  $link"
echo
echo "The server runs in Termux. In Sasonica, Run it on this device → Keep it running"
echo "shows what Android needs so it leaves the server running."
