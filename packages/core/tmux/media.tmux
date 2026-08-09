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

# prefix V → "highlight now": the auto-highlight follow-along normally skips a
# turn if you've typed in the last few seconds (so it doesn't yank copy-mode
# while you're composing). Press this once you've stopped to say "follow this
# one" — it overrides the skip until you next type. (Lowercase `v`, inside the
# popup, toggles the feature on/off; uppercase V here forces the next turn.)
bind V run-shell -b "media highlight-now"

# --- Listening mode -------------------------------------------------------
# A sticky key-table that drives the *speech* channel live with bare keys —
# the popup's controls, minus the popup, so the pane (and the auto-highlight
# follow-along) stays visible while you ride the speed up/down. Enter with
# `prefix L` (lowercase `l` stays last-window) or the bare `M-l`; the table is
# re-armed after every action key so it persists until you press q / Escape
# (or any unbound key, which silently drops back to normal input).
#
# `#{client_key_table}` is `speech` while you're in here — the status line
# keys off that to show the 🎧 indicator (see tmux.conf.local).
bind L switch-client -T speech
bind -n M-l switch-client -T speech

# Speed: [ slower / ] faster (±0.1, clamped), Backspace/0 reset to 1×.
bind -T speech '[' run-shell -b "media speed down"     \; switch-client -T speech
bind -T speech ']' run-shell -b "media speed up"       \; switch-client -T speech
bind -T speech BSpace run-shell -b "media speed reset" \; switch-client -T speech
bind -T speech 0      run-shell -b "media speed reset" \; switch-client -T speech
# Transport: Space play/pause · h/l sentence · H/L paragraph · r replay.
bind -T speech Space run-shell -b "media toggle" \; switch-client -T speech
bind -T speech h run-shell -b "media skip --unit sentence  --dir -1" \; switch-client -T speech
bind -T speech l run-shell -b "media skip --unit sentence  --dir 1"  \; switch-client -T speech
bind -T speech H run-shell -b "media skip --unit paragraph --dir -1" \; switch-client -T speech
bind -T speech L run-shell -b "media skip --unit paragraph --dir 1"  \; switch-client -T speech
bind -T speech r run-shell -b "media replay" \; switch-client -T speech
# Documents: `d` picks one to listen to (the agenda is the first entry), `D`
# goes straight to the agenda. A popup rather than run-shell, because both
# render before they play and a silent 30s is indistinguishable from a hang.
bind -T speech d display-popup -w 90% -h 24 -E "media-popup-docs" \; switch-client -T speech
bind -T speech D display-popup -w 60 -h 8 -E "media doc agenda" \; switch-client -T speech
# Exit (also: any unbound key falls through to normal input).
bind -T speech q      display-message "🎧 listening off"
bind -T speech Escape display-message "🎧 listening off"

# Tell tmux the outer terminal can render OSC 8 hyperlinks, so it forwards
# them instead of stripping them. Without this the `w` web-UI link popup
# (media-popup-link) shows the URL as plain text and a click in Kitty et al.
# hits nothing. `*` covers any OSC 8-capable terminal; ones that don't
# support it ignore the escape, and the popup also prints the raw URL so
# Termux long-press still works. (Takes effect on the next client attach.)
# With `mouse on`, the terminal's mouse is grabbed by tmux, so open the link
# with the emulator's grabbed-app gesture — Kitty: ctrl+shift+click.
set -ga terminal-features '*:hyperlinks'

# Refresh the status bar every second so the `#(media status)` progress
# bar advances smoothly (oh-my-tmux defaults to 10s — too coarse for it).
set -g status-interval 1

# Live progress: add `#(media status 2>/dev/null)` to status-right. Under
# oh-my-tmux, set it in tmux_conf_theme_status_right (the theme rebuilds
# status-right, clobbering an imperative `set -ag`). On a plain tmux,
# uncomment the line below instead.
# set -ag status-right " #(media status 2>/dev/null)"
