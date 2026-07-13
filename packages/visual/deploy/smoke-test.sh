#!/usr/bin/env bash
# Smoke-test the canvas-behind-OWUI wiring.
#
# Checks, in order: the canvas server is up, the reverse proxy serves bg.js and
# the iframe page, the canvas's ROOT endpoints stream same-origin through the
# proxy (the bit the prefix-only spike got wrong), and a pushed image reaches
# the stream. Then prints a short manual checklist for the browser (the
# CSS/legibility part a script can't see).
#
# Usage:  ./smoke-test.sh [PROXY_URL]
#   PROXY_URL  the OWUI-fronting origin, e.g. https://owui.example.ts.net
#              (defaults to the CANVAS server itself, so you can test the
#               canvas alone before Caddy is in front of it — note steps 3/4
#               then hit the canvas directly, which is exactly what the proxy
#               must reproduce same-origin.)
set -euo pipefail

CANVAS="${CANVAS:-http://127.0.0.1:8781}"     # the canvas server (canvas.py)
PROXY="${1:-$CANVAS}"                          # OWUI origin, or canvas if omitted
IMG="${IMG:-https://picsum.photos/1920/1080}"  # any absolute image URL

pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; exit 1; }

echo "== 1. canvas server alive ($CANVAS) =="
curl -sf "$CANVAS/healthz" >/dev/null && pass "healthz ok" || fail "canvas server not answering on $CANVAS"

echo "== 2. bg.js served through the proxy ($PROXY) =="
if curl -sf "$PROXY/canvas/bg.js" | grep -q 'amc-frame'; then
	pass "/canvas/bg.js returns the loader"
else
	fail "/canvas/bg.js not served — check the Caddy 'handle /canvas/bg.js' block + file path"
fi

echo "== 3. the canvas page itself loads through the proxy (the iframe target) =="
curl -sf "$PROXY/canvas/" | grep -qi '<body' && pass "/canvas/ loads" \
	|| fail "/canvas/ not proxied — check 'handle /canvas/*' → :8781"

echo "== 4. the canvas's ROOT endpoints proxy same-origin (the re-port fix) =="
# canvas.js hits these at the ORIGIN root, not under /canvas. If they don't
# reach :8781 through the proxy, the iframe loads but shows a black screen.
# /events is the load-bearing one — the SSE stream. Sample it for a frame.
if curl -sN --max-time 3 "$PROXY/events" | grep -q '^data:'; then
	pass "/events streams SSE frames at the root"
else
	echo "  (no event in 3s — fine if nothing's been shown yet; step 5 forces one)"
fi
# /status is a cheap JSON probe — proves the @canvas root routing at all.
if curl -sf "$PROXY/status?channel=speech" | grep -q '{'; then
	pass "/status reaches the canvas at the root (@canvas routing works)"
else
	fail "/status did not reach :8781 — the iframe's /events, /img, /ctl will all 404 against OWUI. Add the '@canvas' root-route block to the Caddyfile."
fi

echo "== 5. push a test image, confirm it lands on the stream =="
# Start listening at the ROOT /events (same path the iframe uses), then push.
( curl -sN --max-time 5 "$PROXY/events" > /tmp/amc-sse.$$ & echo $! > /tmp/amc-pid.$$ )
sleep 1
curl -sf -X POST "$CANVAS/show" -H 'content-type: application/json' \
	-d "{\"image\":\"$IMG\",\"caption\":\"smoke test — I should fade in behind OWUI\",\"purpose\":\"art\"}" \
	>/dev/null && pass "POST /show accepted" || fail "POST /show rejected (is /show token-guarded? check MEDIA_VISUAL_* env)"
sleep 2
kill "$(cat /tmp/amc-pid.$$)" 2>/dev/null || true
if grep -q "$IMG" /tmp/amc-sse.$$ 2>/dev/null; then
	pass "the pushed image appeared on the SSE stream"
else
	echo "  (didn't see it echoed — the push still worked; the stream sample may have missed it)"
fi
rm -f /tmp/amc-sse.$$ /tmp/amc-pid.$$

# A figure too — exercises the letterbox (contain) + figure path.
curl -sf -X POST "$CANVAS/show" -H 'content-type: application/json' \
	-d "{\"image\":\"$IMG\",\"caption\":\"figure path — should letterbox, not crop\",\"purpose\":\"figure\"}" \
	>/dev/null && pass "figure push accepted" || true

cat <<'EOF'

== 6. now eyeball it in the browser (open your OWUI URL) ==
  [ ] the picsum image is fading in BEHIND the OWUI chat (Ken Burns drifting)
  [ ] OWUI's chat is legible over it — scrim is doing its job
      → if OWUI is opaque and hides the canvas: the transparency selectors in
        bg.js need re-tuning to your OWUI version (html/body/#app + main/messages)
  [ ] tap the ▣ button (bottom-right): the canvas comes forward and the
      audio controller reveals; tap again (or Esc) to send it back
  [ ] with the canvas forward, the WebAudio cues should now be unlocked
  [ ] on the PineNote: load OWUI with ?eink=1 — motion/video off, white page
  [ ] play music via the controller → the muted YouTube mirror appears
      (if it doesn't: that's the iframe's own CSP, inside /canvas/, not OWUI's)
EOF
echo
echo "smoke test done."
