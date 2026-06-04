# agent-media tmux control surface (core `media` CLI).
# Source from your tmux.conf.local (after oh-my-tmux loads):
#     source-file ~/.local/share/agent-media/media.tmux
#
# `prefix a` opens the control popup (top-right). All controls live inside
# the popup; live progress shows in status-right via tmux_conf_theme_status_right
# (see README / tmux.conf.local).
#
# Requires `media` and `media-popup` on PATH (installed in ~/.local/bin).

# Ensure ~/.local/bin is on the tmux *server* PATH. When the server is
# started from a minimal-PATH context (systemd, tmux-continuum resurrect at
# boot), it lacks ~/.local/bin, so the popup below ("media-popup: command
# not found", popup vanishes instantly) and `#(media status)` in the status
# line both silently break. tmux config can't shell-expand $HOME/$PATH, so
# do it via run-shell. Idempotent: only prepends when not already present.
run-shell 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) tmux set-environment -g PATH "$HOME/.local/bin:$PATH" ;; esac'

# prefix a → control popup (top-right), one keystroke. All controls live
# inside the popup (Space play/pause, r replay, h/l seek, -/= volume,
# m mute, [/] speed, v show spoken text, q close). The caller pane (for
# the popup's `v`) is pinned via the global env; the popup also self-
# resolves it, so this is belt-and-suspenders.
# -y is the popup's *bottom* edge (display-menu corner semantics), so with
# -h 4 anything below 4 clamps to the top of the screen. Use 6 to sit the
# popup a couple rows down, clear of a top status line.
# -s/-S bg=default paint the interior + border on the terminal's own
# background rather than a solid fill, so a transparent terminal shows
# through (tmux has no true alpha; this defers to the emulator).
# Width is sized to the calling client (capped at 34) by media-popup-open, so
# the popup never overflows a narrow phone yet stays roomy on a desktop. The
# #{…} formats are expanded by tmux at key-press time (client context).
bind a \
    run-shell -b "media-popup-open '#{client_name}' '#{pane_id}' '#{client_width}' '#{client_height}'"

# Refresh the status bar every second so the `#(media status)` progress
# bar advances smoothly (oh-my-tmux defaults to 10s — too coarse for it).
set -g status-interval 1

# Live progress: add `#(media status 2>/dev/null)` to status-right. Under
# oh-my-tmux, set it in tmux_conf_theme_status_right (the theme rebuilds
# status-right, clobbering an imperative `set -ag`). On a plain tmux,
# uncomment the line below instead.
# set -ag status-right " #(media status 2>/dev/null)"
