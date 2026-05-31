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
bind a \
    display-popup -E -w 24 -h 6 -x R -y 0 "TTS_POPUP_PANE=#{pane_id} media-popup"

# Refresh the status bar every second so the `#(media status)` progress
# bar advances smoothly (oh-my-tmux defaults to 10s — too coarse for it).
set -g status-interval 1

# Live progress: add `#(media status 2>/dev/null)` to status-right. Under
# oh-my-tmux, set it in tmux_conf_theme_status_right (the theme rebuilds
# status-right, clobbering an imperative `set -ag`). On a plain tmux,
# uncomment the line below instead.
# set -ag status-right " #(media status 2>/dev/null)"
