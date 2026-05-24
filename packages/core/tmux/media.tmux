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

# Enter the one-shot `media` key table.
bind T switch-client -T media

# t: pin the caller pane (for the popup's `v` show-text), then open the popup.
# display-popup does not format-expand its command arg, so the pane id is
# passed via the global environment (inherited by the popup's shell).
bind -T media t \
    set-environment -g TTS_POPUP_PANE "#{pane_id}" \; \
    display-popup -E -w 46 -h 5 -x R -y 0 "media-popup"

# One-shot controls straight from the table.
bind -T media Space run-shell -b "media toggle"
bind -T media r     run-shell -b "media replay"

# Live progress: add `#(media status 2>/dev/null)` to status-right. Under
# oh-my-tmux, set it in tmux_conf_theme_status_right (the theme rebuilds
# status-right, clobbering an imperative `set -ag`). On a plain tmux,
# uncomment the line below instead.
# set -ag status-right " #(media status 2>/dev/null)"
