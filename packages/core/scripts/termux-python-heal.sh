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

APPS_CONF="${AGENT_MEDIA_APPS_CONF:-$here/termux-apps.conf}"
APPS_VENVS="${AGENT_MEDIA_APPS_VENVS:-$HOME/.local/venv}"
# The extra apps' entrypoints go where they already live and where the runit
# services name them by absolute path.
APPBIN="$HOME/.local/bin"

mode=repair
case "${1:-}" in
    --check) mode=check ;;
    --force) mode=force ;;
    --install-hook) mode=install-hook ;;
    --apt-repair) mode=apt-repair ;;
    "") ;;
    *) echo "usage: $0 [--check|--force|--apt-repair|--install-hook]" >&2; exit 2 ;;
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

sys_py=$(command -v python3 2>/dev/null)
[ -n "$sys_py" ] || { say "no python3 on PATH"; exit 2; }
sys_ver=$("$sys_py" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)

# --- apt-provided python modules -------------------------------------------
#
# Termux ships some modules as apt packages (pygobject, gst-python, brotli...)
# and upgrading `python` does NOT rebuild them: their files stay in the old
# lib/pythonX.Y/site-packages and simply vanish from the new interpreter's
# view. Mopidy needs gi + gstreamer from exactly there, so agent-media's own
# venv can be perfectly healthy while mopidy is still dead.

stranded_apt_pkgs() {
    [ -d "$PREFIX_DIR/lib" ] || return 0
    command -v dpkg >/dev/null 2>&1 || return 0
    for d in "$PREFIX_DIR"/lib/python3.*/site-packages; do
        [ -d "$d" ] || continue
        case "$d" in *"python$sys_ver"*) continue ;; esac   # the live one
        # One dpkg query for the whole directory; anything owned by a package
        # is a module apt could put back in the right place.
        find "$d" -maxdepth 1 -mindepth 1 2>/dev/null \
            | xargs -r dpkg -S 2>/dev/null \
            | sed 's/:.*//' | tr ',' '\n' | sed 's/^ *//' | sort -u
    done
}

apt_repair() {
    pkgs=$(stranded_apt_pkgs | tr '\n' ' ')
    if [ -z "$(printf '%s' "$pkgs" | tr -d ' ')" ]; then
        say "no apt-provided modules stranded"
        return 0
    fi
    say "apt modules stranded in an old site-packages:$pkgs"
    [ "$mode" = check ] && return 1
    # apt holds its own lock, so this can never run from inside the
    # DPkg::Post-Invoke hook — detach and retry once apt has finished.
    if [ -n "${AGENT_MEDIA_HEAL_FROM_HOOK:-}" ] || \
       ! apt-get check >/dev/null 2>&1; then
        say "apt is busy (in-transaction?); scheduling a detached reinstall"
        setsid sh -c "sleep 60; apt-get install --reinstall -y $pkgs \
            >/dev/null 2>&1" >/dev/null 2>&1 &
        return 0
    fi
    say "reinstalling:$pkgs"
    # shellcheck disable=SC2086
    apt-get install --reinstall -y $pkgs || { say "apt reinstall failed"; return 1; }
    return 0
}

if [ "$mode" = apt-repair ]; then
    apt_repair
    exit $?
fi

[ -d "$CORE" ] || { say "no checkout at $CHECKOUT"; exit 2; }

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
        if [ "$mode" = check ]; then
            say "entrypoints point outside the venv:$stale"
            core_rc=1
        else
            say "relinking entrypoints:$stale"
            for n in $stale; do ln -sfn "$VENV/bin/$n" "$BIN/$n"; done
            core_rc=0
        fi
    else
        core_rc=0
    fi
    [ "${core_rc:-0}" = 0 ] && say "ok — python $sys_ver, editable install, entrypoints linked"
else
    say "repair needed: $reason"
    if [ "$mode" = check ]; then
        core_rc=1
    elif ! command -v uv >/dev/null 2>&1; then
        say "uv not found; cannot rebuild"
        core_rc=2
    else
        core_rc=0
        say "rebuilding $VENV on python $sys_ver"
        # Editable, with deps: a fresh venv has none. uv resolves from wheels,
        # so this is seconds rather than the half-hour source build a system
        # pip install turns into on a phone.
        if ! uv venv --python "$sys_py" "$VENV" >/dev/null 2>&1; then
            say "uv venv failed"; core_rc=1
        elif ! (cd "$CORE" && uv pip install -e . --python "$VENV/bin/python" \
                    >/dev/null 2>&1); then
            say "uv pip install failed"; core_rc=1
        else
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
            else
                say "still broken after rebuild"; core_rc=1
            fi
        fi
    fi
fi

# --- the other apps the same upgrade killed ---------------------------------
#
# mopidy and beets were pip --user installs, so they died exactly as
# agent-media did — and nobody noticed for the same reason. Each gets its own
# venv here (see termux-apps.conf), which makes "repair" mean "rebuild" rather
# than "remember how this was installed".

apps_rc=0

app_ok() {   # name entrypoints -> 0 if this app looks healthy
    _venv="$APPS_VENVS/$1"
    [ -x "$_venv/bin/python" ] || return 1
    [ -n "$sys_ver" ] && [ ! -d "$_venv/lib/python$sys_ver" ] && return 1
    for _e in $2; do
        [ -x "$_venv/bin/$_e" ] || return 1
        # The entrypoint on PATH must be ours, not a stale user-site script.
        [ "$(readlink "$APPBIN/$_e" 2>/dev/null)" = "$_venv/bin/$_e" ] || return 1
    done
    return 0
}

heal_app() {  # name flags entrypoints requirements...
    _name=$1; _flags=$2; _entry=$3; shift 3
    _venv="$APPS_VENVS/$_name"
    # Only ever ADOPT an app this host already has — never install one. red5
    # runs its own Mopidy (the whole-house stream); a heal run there must not
    # build a second copy and hijack the command out from under it.
    if [ ! -d "$_venv" ]; then
        _present=0
        for _e in $_entry; do
            command -v "$_e" >/dev/null 2>&1 && _present=1
        done
        if [ "$_present" = 0 ]; then
            return 0
        fi
    fi
    if app_ok "$_name" "$_entry" && [ "$mode" != force ]; then
        say "app $_name: ok"
        return 0
    fi
    if [ "$mode" = check ]; then
        say "app $_name: needs repair"
        return 1
    fi
    command -v uv >/dev/null 2>&1 || { say "app $_name: uv missing"; return 2; }
    mkdir -p "$APPS_VENVS" "$APPBIN" 2>/dev/null
    say "app $_name: rebuilding $_venv"
    # Start clean: uv refuses to create over an existing venv, so a half-built
    # one from an interrupted run would wedge every later attempt.
    case "$_venv" in
        "$APPS_VENVS"/?*) rm -rf "$_venv" ;;
        *) say "app $_name: refusing to remove $_venv"; return 1 ;;
    esac
    # shellcheck disable=SC2086
    if ! uv venv $_flags --python "$sys_py" "$_venv" >/dev/null 2>&1; then
        say "app $_name: uv venv failed"; return 1
    fi
    # shellcheck disable=SC2086
    if ! uv pip install --python "$_venv/bin/python" $* >/dev/null 2>&1; then
        say "app $_name: install failed"; return 1
    fi
    for _e in $_entry; do
        [ -x "$_venv/bin/$_e" ] || { say "app $_name: no entrypoint $_e"; return 1; }
        ln -sfn "$_venv/bin/$_e" "$APPBIN/$_e"
    done
    say "app $_name: repaired ($_entry)"
    return 0
}

# Termux only: this whole section exists because Termux upgrades its
# interpreter in place. A normal distro's python packaging doesn't strand
# installs this way, and adopting a distro's apps into private venvs would be
# an unpleasant surprise on red5 or pn.
if [ -r "$APPS_CONF" ] && [ -d /data/data/com.termux/files/usr ]; then
    # `while read` in a pipeline would run in a subshell on some shells and
    # lose apps_rc, so feed it from a redirect.
    while IFS='|' read -r name flags entry reqs; do
        case "$name" in ''|\#*) continue ;; esac
        name=$(printf '%s' "$name" | tr -d ' ')
        [ -n "$name" ] || continue
        heal_app "$name" "$flags" "$entry" $reqs || apps_rc=1
    done < "$APPS_CONF"
fi

# Report (never auto-run) the apt side: mopidy's gi/gstreamer come from apt
# packages that a python upgrade strands too, and no venv can substitute for
# them. Repairing needs `--apt-repair`, which can't run inside the apt hook.
apt_stranded=$(stranded_apt_pkgs | tr '\n' ' ')
if [ -n "$(printf '%s' "$apt_stranded" | tr -d ' ')" ]; then
    say "apt modules still stranded:$apt_stranded"
    say "run: $0 --apt-repair"
    apps_rc=1
fi

case "${core_rc:-0}$apps_rc" in
    00) exit 0 ;;
    *) [ "${core_rc:-0}" = 2 ] && exit 2; exit 1 ;;
esac
