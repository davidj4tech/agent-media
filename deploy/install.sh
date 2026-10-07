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
# The platform installer is fetched to a file and run from there with the
# terminal as its stdin (when there is one), not the pipe this came in on.
# Safe to run again: that is how the binary is updated.
#
# SASONICA_INSTALL_BASE overrides where the installers are fetched from
# (tests: a file:// directory); SASONICA_BINARY_BASE where the binary is.
set -eu

BASE=${SASONICA_INSTALL_BASE:-https://raw.githubusercontent.com/davidj4tech/agent-media/main/deploy}
BINARY_BASE=${SASONICA_BINARY_BASE:-https://github.com/davidj4tech/agent-media/releases/download/server-latest}

case "${PREFIX:-}" in
  /data/data/com.termux/*)
    script="${TMPDIR:-$PREFIX/tmp}/sasonica-android-install.sh"
    curl -fsSL "$BASE/android/install.sh" -o "$script"
    # Piped from curl, stdin is this script: give the installer the terminal.
    if ( : </dev/tty ) 2>/dev/null; then exec bash "$script" </dev/tty; fi
    exec bash "$script"
    ;;
esac

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
  # A rename, not a copy over: a running server keeps the file it started from.
  mv -f "$bin/sasonica.new" "$bin/sasonica"
  "$bin/sasonica" version

  if ( : </dev/tty ) 2>/dev/null; then "$bin/sasonica" install </dev/tty; else "$bin/sasonica" install; fi

  printf '\n\033[1m== %s\033[0m\n' "Pairing"
  up=
  for _ in $(seq 1 30); do
    if curl -fsS -m 2 http://127.0.0.1:8781/healthz >/dev/null 2>&1; then up=1; break; fi
    sleep 1
  done
  if [ -z "$up" ]; then
    echo "The server is not answering on port 8781 yet; when it is, pair with:"
    echo "  sasonica media-visual-canvas pair --device \"$(hostname)\""
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
      echo "  sasonica media-visual-canvas pair --device \"$(hostname)\""
      echo "(its log: journalctl --user -u sasonica-quick-tunnel, or ~/Library/Logs/sasonica)"
      return 0
    fi
  fi
  echo "Scan this with Sasonica (Pair a server), or paste the link into its pairing screen:"
  "$bin/sasonica" media-visual-canvas pair --device "$(hostname)" || {
    echo "Pair again once the phone can reach this computer (see above)."
    return 0
  }
}

case "$(uname -s 2>/dev/null)" in
  Linux)
    unix linux
    ;;
  Darwin)
    unix macos
    ;;
  *)
    echo "Sasonica does not install on $(uname -s 2>/dev/null || echo this system) yet." >&2
    exit 1
    ;;
esac
