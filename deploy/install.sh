#!/bin/sh
# sasonica.com/install — one line for any device:
#
#   curl -fsSL https://sasonica.com/install | bash
#
# The address cannot tell where curl runs, so this script does, and hands
# over to that platform's installer:
#   - Termux (Android: phone, tablet, TV box, Chromebook) → android/install.sh
#   - a computer → not yet: says so, and where the manual setup is.
#
# The platform installer is fetched to a file and run from there with the
# terminal as its stdin (when there is one), not the pipe this came in on.
#
# SASONICA_INSTALL_BASE overrides where the installers are fetched from
# (tests: a file:// directory).
set -eu

BASE=${SASONICA_INSTALL_BASE:-https://raw.githubusercontent.com/davidj4tech/agent-media/main/deploy}

case "${PREFIX:-}" in
  /data/data/com.termux/*)
    script="${TMPDIR:-$PREFIX/tmp}/sasonica-android-install.sh"
    curl -fsSL "$BASE/android/install.sh" -o "$script"
    # Piped from curl, stdin is this script: give the installer the terminal.
    if ( : </dev/tty ) 2>/dev/null; then exec bash "$script" </dev/tty; fi
    exec bash "$script"
    ;;
esac

case "$(uname -s 2>/dev/null)" in
  Linux | Darwin)
    cat <<'EOF'
Sasonica's one-line install is for Android so far: run it in Termux on the
device, and the Sasonica app pairs with it by itself.

On a computer, set up agent-media by hand for now:
  https://github.com/davidj4tech/agent-media#readme
EOF
    exit 1
    ;;
  *)
    echo "Sasonica does not install on $(uname -s 2>/dev/null || echo this system) yet." >&2
    exit 1
    ;;
esac
