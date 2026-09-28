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
#     `sasonica install` (shims, config, the agents' hooks, two systemd --user
#     services) and a pairing QR code for the app;
#   - a Mac → not yet: says so, and where the manual setup is.
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

# Linux: the binary. Its launcher needs glibc (2.31+), not musl.
linux() {
  case "$(uname -m)" in
    x86_64 | amd64) arch=x86_64 ;;
    aarch64 | arm64) arch=aarch64 ;;
    *) echo "Sasonica's server is not built for $(uname -m) yet." >&2; exit 1 ;;
  esac
  if ldd --version 2>&1 | grep -qi musl; then
    echo "Sasonica's server needs glibc; this system uses musl (Alpine?)." >&2; exit 1
  fi
  bin="$HOME/.local/bin"
  mkdir -p "$bin"
  printf '\n\033[1m== %s\033[0m\n' "Sasonica's server ($arch)"
  curl -fL --progress-bar "$BINARY_BASE/sasonica-linux-$arch" -o "$bin/sasonica.new"
  want=$(curl -fsSL "$BINARY_BASE/SHA256SUMS" | awk -v f="sasonica-linux-$arch" '$2 == f {print $1}')
  got=$(sha256sum "$bin/sasonica.new" | cut -d' ' -f1)
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
  echo "Scan this with Sasonica (Pair a server), or paste the link into its pairing screen:"
  "$bin/sasonica" media-visual-canvas pair --device "$(hostname)"
}

case "$(uname -s 2>/dev/null)" in
  Linux)
    linux
    ;;
  Darwin)
    cat <<'EOF'
Sasonica's one-line install is for Android and Linux so far.

On a Mac, set up agent-media by hand for now:
  https://github.com/davidj4tech/agent-media#readme
EOF
    exit 1
    ;;
  *)
    echo "Sasonica does not install on $(uname -s 2>/dev/null || echo this system) yet." >&2
    exit 1
    ;;
esac
