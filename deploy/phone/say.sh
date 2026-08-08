#!/data/data/com.termux/files/usr/bin/sh
# Render a reply on THIS device and play it through the local sink-speech
# broker. Invoked over ssh by agent-media's remote-say (MEDIA_REMOTE_SAY_CMD).
#
# Engine and voice are not baked in: rendering goes through agent_media_core's
# render registry, so MEDIA_RENDER_ENGINE and MEDIA_RENDER_VOICE_<ENGINE> mean
# the same thing here as on any other host, including installed engine plugins.
# Config is read from this device's ~/.config/agent-media.env, so the phone can
# use a different voice from the machine that sent the text.
#
# Playback goes through the long-running sink-speech mpv rather than a one-shot
# `mpv file`, because a one-shot has no IPC socket: pause, resume and skip from
# the popup have nothing to talk to and silently do nothing.
#
# Text in on stdin. One line out, immediately before playback:
#     DURATION <seconds>
# That plus the local clock is what draws the caller's progress bar. Keep
# stdout otherwise quiet.
set -eu

VENV="${MEDIA_VENV:-$HOME/projects/agent-media/.venv}"
SOCK="${MEDIA_SPEECH_SOCK:-$HOME/.local/state/agent-media/sink-speech.sock}"
CLIP="${MEDIA_SAY_CLIP:-$HOME/.cache/agent-media-say.mp3}"

text=$(cat)
[ -n "$text" ] || exit 0

mkdir -p "$(dirname "$CLIP")"

# Core's registry picks the engine, applies the per-engine voice, and falls back
# to edge exactly as it does on any other host.
#
# The text goes via a file, not a pipe: `python - <<PY` already uses stdin for
# the program itself, so a piped stdin is swallowed and the script renders
# silence.
txt=$(mktemp "$HOME/.cache/agent-media-say.XXXXXX.txt")
trap 'rm -f "$txt"' EXIT INT TERM
printf '%s' "$text" > "$txt"

PATH="$VENV/bin:$PATH"; export PATH   # engines shell out to edge-tts et al

"$VENV/bin/python" - "$CLIP" "$txt" <<'PYEOF' >/dev/null 2>&1
import os, pathlib, sys
from agent_media_core.intake._env import load_env_file
load_env_file("remote-say")
from agent_media_core.render import render_text

out = pathlib.Path(sys.argv[1])
text = pathlib.Path(sys.argv[2]).read_text()
engine = os.environ.get("MEDIA_RENDER_ENGINE", "edge")
voice = os.environ.get("MEDIA_RENDER_VOICE_" + engine.upper()) or None
ok, _ = render_text(text, out, engine=engine, voice=voice)
sys.exit(0 if ok else 1)
PYEOF

[ -s "$CLIP" ] || exit 1

dur=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$CLIP" 2>/dev/null || true)
case "$dur" in ''|*[!0-9.]*) ;; *) printf 'DURATION %s\n' "$dur" ;; esac

ipc() { printf '%s\n' "$1" | timeout 10 socat - "UNIX-CONNECT:$SOCK" 2>/dev/null; }

# No broker: still speak, just uncontrollably. Silence would be worse.
if [ ! -S "$SOCK" ]; then
    exec mpv --no-video --really-quiet --no-terminal "$CLIP" >/dev/null 2>&1
fi

# A reply must be audible regardless of a pause or mute left on the broker.
ipc '{"command":["set_property","pause",false]}' >/dev/null
ipc '{"command":["set_property","mute",false]}' >/dev/null
ipc "{\"command\":[\"loadfile\",\"$CLIP\",\"replace\"]}" >/dev/null

# Block until it finishes so the caller's before/after-speech bracketing (and
# music ducking) spans the actual audio. Polling is local — ~2ms a check here
# versus ~500ms from the far end — and bounded, so a wedged broker cannot hang
# the caller's hook.
#
# The bound comes from the clip, not from a constant. It was a flat 600 ticks
# (~120s), which is shorter than plenty of real replies: a 2000-character
# answer renders to nearly three minutes of audio, so the wait expired
# mid-sentence, every timeout above this one fired in turn, and the reply was
# cut off with mpv left paused halfway through. A bound on how long we'll wait
# for a *known* quantity should be derived from it. Slack covers the poll
# interval and a slow device; the fallback is the old constant, for when
# ffprobe couldn't measure the clip.
ticks=600                                  # 0.2s each
case "$dur" in
    ''|*[!0-9.]*) ;;
    *) ticks=$(awk -v d="$dur" 'BEGIN{printf "%d", (d + 30) * 5}') ;;
esac
waited=0
sleep 0.4
while [ "$waited" -lt "$ticks" ]; do
    case "$(ipc '{"command":["get_property","idle-active"]}')" in
        *'"data":true'*) break ;;
    esac
    sleep 0.2
    waited=$((waited + 1))
done
