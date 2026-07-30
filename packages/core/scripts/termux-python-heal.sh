#!/bin/sh
# Re-home the agent-media install after Termux upgrades its Python.
#
# Termux upgrades python in place: `python3` becomes 3.14 while everything ever
# installed lives in .../lib/python3.13/site-packages, which is now invisible.
# On 2026-07-30 that silently killed every agent-media entrypoint on the phone
# (media-mcp crash-looped ~1250 times against `ModuleNotFoundError`) while the
# git checkout looked perfectly healthy.
#
# The layout this script maintains and repairs:
#
#   <checkout>/.venv                  the one install, editable, tracking git
#   $PREFIX/bin/media*  ->  symlinks into <checkout>/.venv/bin/
#
# so a plain `git pull` is a complete deploy and nothing depends on the system
# interpreter's site-packages.
#
# Usage:
#   termux-python-heal.sh                 check, and repair if needed
#   termux-python-heal.sh --check         report only, exit 1 if repair needed
#   termux-python-heal.sh --force         rebuild the venv unconditionally
#   termux-python-heal.sh --install-hook  register the apt post-invoke hook
#
# POSIX sh with no agent-media imports on purpose: it has to run when the
# package it repairs cannot be imported.
set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
CHECKOUT=${AGENT_MEDIA_CHECKOUT:-$(CDPATH= cd -- "$here/../../.." && pwd -P)}
VENV="$CHECKOUT/.venv"
CORE="$CHECKOUT/packages/core"
PREFIX_DIR="${PREFIX:-/data/data/com.termux/files/usr}"
# Termux keeps console scripts in $PREFIX/bin; elsewhere (red5, pn) they land in
# ~/.local/bin. Both are "the dir whose media* must resolve into this venv".
if [ -d "$PREFIX_DIR/bin" ]; then
    BIN="$PREFIX_DIR/bin"
else
    BIN="$HOME/.local/bin"
fi

mode=repair
case "${1:-}" in
    --check) mode=check ;;
    --force) mode=force ;;
    --install-hook) mode=install-hook ;;
    "") ;;
    *) echo "usage: $0 [--check|--force|--install-hook]" >&2; exit 2 ;;
esac

say() { echo "python-heal: $*"; }

install_hook() {
    conf_dir="$PREFIX_DIR/etc/apt/apt.conf.d"
    if [ ! -d "$conf_dir" ]; then
        say "no apt conf dir at $conf_dir — not a Termux host; nothing to do"
        return 1
    fi
    conf="$conf_dir/99-agent-media-python-heal"
    # Post-Invoke runs after every apt transaction; the script itself is cheap
    # when nothing changed (one version compare, one import), so there's no
    # need to work out whether *this* transaction touched python.
    cat > "$conf" <<EOF
// Installed by agent-media (packages/core/scripts/termux-python-heal.sh).
// Termux upgrades python in place, stranding site-packages and every console
// script with it; this re-homes the agent-media venv afterwards.
DPkg::Post-Invoke { "sh $here/termux-python-heal.sh >/dev/null 2>&1 || true"; };
EOF
    say "installed apt hook: $conf"
    return 0
}

if [ "$mode" = install-hook ]; then
    install_hook
    exit $?
fi

[ -d "$CORE" ] || { say "no checkout at $CHECKOUT"; exit 2; }

sys_py=$(command -v python3 2>/dev/null)
[ -n "$sys_py" ] || { say "no python3 on PATH"; exit 2; }
sys_ver=$("$sys_py" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)

needs_repair=0
reason=""

# 1. Does the venv still run at all? A venv whose interpreter was upgraded out
#    from under it fails here.
if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -c 'import sys' 2>/dev/null; then
    needs_repair=1; reason="venv interpreter is gone or broken"
# 2. Does it match the system python? (lib/pythonX.Y is the venv's own tree.)
elif [ -n "$sys_ver" ] && [ ! -d "$VENV/lib/python$sys_ver" ]; then
    needs_repair=1; reason="venv is not python $sys_ver"
# 3. Can it import the package?
elif ! "$VENV/bin/python" -c 'import agent_media_core' 2>/dev/null; then
    needs_repair=1; reason="agent_media_core does not import"
# 4. Is the install editable — i.e. does a `git pull` actually deploy? A
#    non-editable copy is the quiet failure: everything works, but it runs
#    whatever the code was on install day. That's exactly how call-guard ran
#    stale for weeks.
else
    mod=$("$VENV/bin/python" -c 'import agent_media_core,os;print(os.path.realpath(agent_media_core.__file__))' 2>/dev/null)
    case "$mod" in
        "$CORE"/src/*) ;;
        *) needs_repair=1; reason="install is a copy, not editable ($mod)" ;;
    esac
fi

[ "$mode" = force ] && { needs_repair=1; reason="--force"; }

if [ "$needs_repair" = 0 ]; then
    # Entrypoints can rot on their own (a new console script in pyproject that
    # was never linked), so check them even on a healthy venv.
    # Every media* already on PATH must resolve into THIS venv — either as a
    # symlink to the venv script (what a repair creates) or as a console script
    # whose shebang is the venv's python (what pip/uv write, and what red5 and
    # pn already have). A stale one pointing at a dead interpreter is the
    # breakage; a venv script that was simply never linked into BIN is not —
    # unused entrypoints aren't a fault, and flagging them here would make the
    # check cry wolf on every host. A service that actually needs a missing one
    # fails to start, which `media doctor` reports separately.
    stale=""
    for t in "$BIN"/media*; do
        [ -e "$t" ] || continue
        n=$(basename "$t")
        [ -e "$VENV/bin/$n" ] || continue      # orphan of a removed entrypoint
        if [ "$(readlink "$t" 2>/dev/null)" = "$VENV/bin/$n" ]; then
            continue
        elif [ -f "$t" ] && head -1 "$t" 2>/dev/null | grep -qF "$VENV/bin/"; then
            continue
        fi
        stale="$stale $n"
    done
    if [ -n "$stale" ]; then
        [ "$mode" = check ] && { say "entrypoints point outside the venv:$stale"; exit 1; }
        say "relinking entrypoints:$stale"
        for n in $stale; do ln -sfn "$VENV/bin/$n" "$BIN/$n"; done
    fi
    say "ok — python $sys_ver, editable install, entrypoints linked"
    exit 0
fi

say "repair needed: $reason"
[ "$mode" = check ] && exit 1

command -v uv >/dev/null 2>&1 || { say "uv not found; cannot rebuild"; exit 2; }

say "rebuilding $VENV on python $sys_ver"
uv venv --python "$sys_py" "$VENV" >/dev/null 2>&1 \
    || { say "uv venv failed"; exit 1; }
# Editable, with deps: a fresh venv has none. uv resolves from wheels, so this
# is seconds rather than the half-hour source build a system pip install turns
# into on a phone.
(cd "$CORE" && uv pip install -e . --python "$VENV/bin/python" >/dev/null 2>&1) \
    || { say "uv pip install failed"; exit 1; }

n=0
for f in "$VENV"/bin/media*; do
    [ -e "$f" ] || continue
    ln -sfn "$f" "$BIN/$(basename "$f")" && n=$((n + 1))
done
say "relinked $n entrypoint(s) into $BIN"

if "$VENV/bin/python" -c 'import agent_media_core' 2>/dev/null; then
    say "repaired"
    if command -v termux-notification >/dev/null 2>&1; then
        termux-notification --id agent-media:python-heal \
            --title "agent-media: install repaired" \
            --content "python $sys_ver — venv rebuilt ($reason)" 2>/dev/null
    fi
    exit 0
fi
say "still broken after rebuild"
exit 1
