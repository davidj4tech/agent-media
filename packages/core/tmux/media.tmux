# agent-media tmux control surface (core `media` CLI).
# Source from your tmux.conf.local (after oh-my-tmux loads):
#     source-file ~/.local/share/agent-media/media.tmux
#
# `prefix T` enters the `media` key table for one keystroke:
#     t      → control popup (top-right)
#     Space  → play/pause toggle
#     r      → replay latest clip
# and appends a live progress indicator to status-right.
#
# Requires `media` and `media-popup` on PATH (installed in ~/.local/bin).

# prefix T → control popup (top-right). All controls live inside the popup
# (Space play/pause, r replay, h/l seek, -/= volume, m mute, [/] speed,
# v show spoken text, q close). One keystroke, not a sub-table.
# The caller pane (for the popup's `v`) is pinned via the global env; the
# popup also self-resolves it, so this is belt-and-suspenders.
bind T \
    set-environment -g TTS_POPUP_PANE "#{pane_id}" \; \
    display-popup -E -w 46 -h 6 -x R -y 0 "media-popup"

# Live progress: add `#(media status 2>/dev/null)` to status-right. Under
# oh-my-tmux, set it in tmux_conf_theme_status_right (the theme rebuilds
# status-right, clobbering an imperative `set -ag`). On a plain tmux,
# uncomment the line below instead.
# set -ag status-right " #(media status 2>/dev/null)"
