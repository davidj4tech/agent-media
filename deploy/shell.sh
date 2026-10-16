#!/bin/sh
# sasonica.com/shell — Sasonica Shell alone, no server, on Linux or a Mac:
#
#   curl -fsSL https://sasonica.com/shell | bash
#
# The twin of Windows' sasonica.com/shell.ps1 (sasonica-shell's get.ps1). It
# is install.sh with SASONICA_ONLY=shell, the same as
#
#   curl -fsSL https://sasonica.com/install | bash -s shell
#
# which a redirect alone cannot say: Shell's tarball to
# ~/.local/share/sasonica/shell, its install.sh --hosted, a link and a code
# to sign in with a Sasonica account. David, 16 Oct 2026.
#
# SASONICA_INSTALL_BASE overrides where install.sh is fetched from (tests).
set -eu

BASE=${SASONICA_INSTALL_BASE:-https://raw.githubusercontent.com/davidj4tech/agent-media/main/deploy}
script=$(mktemp "${TMPDIR:-/tmp}/sasonica-install.XXXXXX")
trap 'rm -f "$script"' EXIT
curl -fsSL "$BASE/install.sh" -o "$script"
# Not exec: the trap removes the file after. install.sh asks /dev/tty itself.
SASONICA_ONLY=shell bash "$script"
