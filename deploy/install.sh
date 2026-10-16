#!/bin/sh
# sasonica.com/install — one line for any device:
#
#   curl -fsSL https://sasonica.com/install | bash
#
# The address cannot tell where curl runs, so this script does, and hands
# over to that platform's installer:
#   - Termux (Android: phone, tablet, TV box, Chromebook) → android/install.sh
#   - Linux (x86_64, arm64) → the server as one file, from the server-latest
#     release (deploy/binary/build.sh), put at ~/.local/bin/sasonica; then
#     `sasonica install` (shims, config, the agents' hooks, a quick tunnel,
#     systemd --user services) and a pairing QR code for the app, naming the
#     tunnel's https address;
#   - a Mac (Apple silicon) → the same, as sasonica-macos-aarch64, with
#     launchd agents for the services.
#
# Then, on Linux and a Mac, one question, default No (David, 16 Oct 2026):
# also install Sasonica Shell, which lets an AI assistant such as Claude run
# commands on this computer through the hosted relay. It needs Node (Shell's
# own installer gets it) and a free Sasonica account: Shell prints a link and
# a code to sign in with. Its repository is downloaded as a tarball to
# ~/.local/share/sasonica/shell (no git) and its install.sh --hosted run from
# there. The question is read from the terminal (/dev/tty: under curl | bash
# stdin is this script); with no terminal the answer is No. A Shell that does
# not install does not fail the server's install: it says how to try again.
#
#   SASONICA_WITH_SHELL=1   install Shell without asking; 0: skip it, no question
#   SASONICA_ONLY=shell     Shell alone, no server (no question: you asked);
#                           the same as `bash -s shell`, and sasonica.com/shell
#                           (deploy/shell.sh):
#                             curl -fsSL https://sasonica.com/install | bash -s shell
#
# Shell's command is `sasonica` too: where the server is ~/.local/bin/sasonica,
# Shell's is ~/.local/bin/sasonica-shell (moved there, and the server's binary
# put back, if Shell's installer wrote over it).
#
# The platform installer is fetched to a file and run from there with the
# terminal as its stdin (when there is one), not the pipe this came in on.
# Safe to run again: that is how the binary is updated.
#
# SASONICA_INSTALL_BASE overrides where the installers are fetched from
# (tests: a file:// directory); SASONICA_BINARY_BASE where the binary is;
# SASONICA_SHELL_TARBALL where Shell is (a branch, a fork, a test's stub).
set -eu

BASE=${SASONICA_INSTALL_BASE:-https://raw.githubusercontent.com/davidj4tech/agent-media/main/deploy}
BINARY_BASE=${SASONICA_BINARY_BASE:-https://github.com/davidj4tech/agent-media/releases/download/server-latest}
SHELL_TARBALL=${SASONICA_SHELL_TARBALL:-https://github.com/davidj4tech/sasonica-shell/archive/refs/heads/main.tar.gz}

ONLY=${SASONICA_ONLY:-}
if [ "${1:-}" = shell ]; then ONLY=shell; fi
case "$ONLY" in
  '' | shell) ;;
  *) echo "SASONICA_ONLY is 'shell' or unset, not '$ONLY'." >&2; exit 2 ;;
esac

case "${PREFIX:-}" in
  /data/data/com.termux/*)
    if [ "$ONLY" = shell ]; then
      echo "Sasonica Shell installs on Linux, a Mac and Windows; not on Android." >&2; exit 1
    fi
    script="${TMPDIR:-$PREFIX/tmp}/sasonica-android-install.sh"
    curl -fsSL "$BASE/android/install.sh" -o "$script"
    # Piped from curl, stdin is this script: give the installer the terminal.
    if ( : </dev/tty ) 2>/dev/null; then exec bash "$script" </dev/tty; fi
    exec bash "$script"
    ;;
esac

tty_ok() { ( : </dev/tty ) 2>/dev/null; }

# Sasonica Shell's own command, ~/.local/bin/sasonica where it is alone: a
# two-line sh shim that runs its sasonica.mjs under Node.
is_shell_shim() { [ -f "$1" ] && head -c 400 "$1" 2>/dev/null | grep -q 'sasonica\.mjs'; }

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi; }

# Linux or a Mac: the binary. On Linux its launcher needs glibc (2.31+), not musl.
unix() {
  os=$1
  case "$(uname -m)" in
    x86_64 | amd64) arch=x86_64 ;;
    aarch64 | arm64) arch=aarch64 ;;
    *) echo "Sasonica's server is not built for $(uname -m) yet." >&2; exit 1 ;;
  esac
  if [ "$os" = macos ] && [ "$arch" != aarch64 ]; then
    echo "Sasonica's server is built for Apple silicon Macs only so far." >&2; exit 1
  fi
  if [ "$os" = linux ] && ldd --version 2>&1 | grep -qi musl; then
    echo "Sasonica's server needs glibc; this system uses musl (Alpine?)." >&2; exit 1
  fi
  bin="$HOME/.local/bin"
  mkdir -p "$bin"
  printf '\n\033[1m== %s\033[0m\n' "Sasonica's server ($arch)"
  curl -fL --progress-bar "$BINARY_BASE/sasonica-$os-$arch" -o "$bin/sasonica.new"
  want=$(curl -fsSL "$BINARY_BASE/SHA256SUMS" | awk -v f="sasonica-$os-$arch" '$2 == f {print $1}')
  got=$(sha256 "$bin/sasonica.new" | cut -d' ' -f1)
  if [ -z "$want" ] || [ "$got" != "$want" ]; then
    rm -f "$bin/sasonica.new"
    echo "install: the download does not match its checksum; nothing was changed." >&2; exit 1
  fi
  chmod +x "$bin/sasonica.new"
  # Shell, installed first, had this name: it moves aside, still working.
  if is_shell_shim "$bin/sasonica"; then
    mv -f "$bin/sasonica" "$bin/sasonica-shell"
    echo "Sasonica Shell's command is now sasonica-shell (sasonica is the server)."
  fi
  # A rename, not a copy over: a running server keeps the file it started from.
  mv -f "$bin/sasonica.new" "$bin/sasonica"
  "$bin/sasonica" version

  if tty_ok; then "$bin/sasonica" install </dev/tty; else "$bin/sasonica" install; fi

  printf '\n\033[1m== %s\033[0m\n' "Pairing"
  up=
  for _ in $(seq 1 30); do
    if curl -fsS -m 2 http://127.0.0.1:8781/healthz >/dev/null 2>&1; then up=1; break; fi
    sleep 1
  done
  if [ -z "$up" ]; then
    echo "The server is not answering on port 8781 yet; when it is, pair with:"
    echo "  sasonica pair --device \"$(hostname)\""
    return 0
  fi
  # The quick tunnel (sasonica install, where the phone has no other way in):
  # pair with its https address once cloudflared has named it.
  if [ -f "$HOME/.config/systemd/user/sasonica-quick-tunnel.service" ] \
     || [ -f "$HOME/Library/LaunchAgents/com.sasonica.quick-tunnel.plist" ]; then
    if url=$("$bin/sasonica" media-tunnel url --wait 60); then
      echo "The phone reaches this computer at $url (a Cloudflare quick tunnel)."
      for _ in $(seq 1 30); do
        if curl -fsS -m 5 "$url/healthz" >/dev/null 2>&1; then break; fi
        sleep 2
      done
    else
      echo "The quick tunnel has not started yet; when it has, pair with:"
      echo "  sasonica pair --device \"$(hostname)\""
      echo "(its log: journalctl --user -u sasonica-quick-tunnel, or ~/Library/Logs/sasonica)"
      return 0
    fi
  fi
  echo "Scan this with Sasonica (Pair a server), or paste the link into its pairing screen:"
  "$bin/sasonica" pair --device "$(hostname)" || {
    echo "Pair again once the phone can reach this computer (see above)."
    return 0
  }
}

# Shell or not, once the server is in. SASONICA_WITH_SHELL answers without a
# question; else the terminal is asked, and no terminal is No.
want_shell() {
  case "${SASONICA_WITH_SHELL:-}" in
    1 | y | Y | yes | true) return 0 ;;
    0 | n | N | no | false) return 1 ;;
  esac
  tty_ok || return 1
  printf '\nAlso let an AI assistant such as Claude run commands on this computer\n(Sasonica Shell)? It needs a free Sasonica account. [y/N] ' >/dev/tty
  answer=
  read -r answer </dev/tty || return 1
  case "$answer" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

# Shell's repository as a tarball (no git), unpacked to
# ~/.local/share/sasonica/shell, then its install.sh --hosted: Node if it is
# missing, a link and a code to sign in with, a systemd --user unit or a
# launchd agent (sasonica-shell / com.sasonica.shell). Like Shell's get.ps1 on
# Windows: an update replaces the files and keeps install.conf and
# worker/wrangler.jsonc; the config in ~/.config/sasonica stays. Every step
# checked by hand: this runs where set -e does not (shell_step's `if`).
shell_install() {
  dest="${XDG_DATA_HOME:-$HOME/.local/share}/sasonica/shell"
  tmp=$(mktemp -d) || return 1
  if ! curl -fsSL "$SHELL_TARBALL" -o "$tmp/shell.tar.gz" || ! mkdir "$tmp/x" \
     || ! tar -xzf "$tmp/shell.tar.gz" -C "$tmp/x"; then
    echo "Could not download Sasonica Shell from $SHELL_TARBALL." >&2
    rm -rf "$tmp"; return 1
  fi
  src=$(find "$tmp/x" -mindepth 1 -maxdepth 1 -type d | head -n 1)
  if [ -z "$src" ] || [ ! -f "$src/install.sh" ]; then
    echo "The download from $SHELL_TARBALL has no install.sh." >&2
    rm -rf "$tmp"; return 1
  fi
  for k in install.conf worker/wrangler.jsonc; do
    if [ -f "$dest/$k" ]; then mkdir -p "$(dirname "$src/$k")" && cp -p "$dest/$k" "$src/$k"; fi
  done
  rm -rf "$dest"
  mkdir -p "$(dirname "$dest")" && mv "$src" "$dest" || { rm -rf "$tmp"; return 1; }
  rm -rf "$tmp"
  echo "Unpacked to $dest."

  # Shell writes its command to ~/.local/bin/sasonica, the server's binary:
  # keep the server's (a hard link, the same file) and put it back after.
  bin="$HOME/.local/bin"
  saved=
  if [ -f "$bin/sasonica" ] && ! is_shell_shim "$bin/sasonica"; then
    saved="$bin/.sasonica-server.$$"
    ln -f "$bin/sasonica" "$saved" 2>/dev/null || cp -p "$bin/sasonica" "$saved" || saved=
  fi
  echo "Shell prints a link and a code: open the link, sign in with your Sasonica"
  echo "account (or make one, free), check the code matches and press Join."
  rc=0
  if tty_ok; then bash "$dest/install.sh" --hosted </dev/tty || rc=$?
  else bash "$dest/install.sh" --hosted || rc=$?; fi
  if [ -n "$saved" ]; then
    if cmp -s "$saved" "$bin/sasonica"; then
      rm -f "$saved"
    else
      if is_shell_shim "$bin/sasonica"; then mv -f "$bin/sasonica" "$bin/sasonica-shell"; fi
      mv -f "$saved" "$bin/sasonica"
    fi
    if [ -f "$bin/sasonica-shell" ]; then
      echo "Sasonica Shell's command here is sasonica-shell (sasonica is the server)."
    fi
  fi
  return "$rc"
}

shell_step() {
  printf '\n\033[1m== %s\033[0m\n' "Sasonica Shell"
  if shell_install; then return 0; fi
  if [ -z "$ONLY" ]; then
    echo "Sasonica Shell did not install; the server is installed and is not affected."
  else
    echo "Sasonica Shell did not install."
  fi
  echo "To try again:"
  echo "  curl -fsSL https://sasonica.com/install | bash -s shell"
  return 1
}

case "$(uname -s 2>/dev/null)" in
  Linux) os=linux ;;
  Darwin) os=macos ;;
  *)
    echo "Sasonica does not install on $(uname -s 2>/dev/null || echo this system) yet." >&2
    exit 1
    ;;
esac

if [ "$ONLY" = shell ]; then
  shell_step || exit 1
  exit 0
fi
unix "$os"
if want_shell; then shell_step || true; fi
