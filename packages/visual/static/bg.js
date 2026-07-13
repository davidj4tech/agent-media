/* agent-media canvas — full canvas behind Open WebUI, via a same-origin iframe.
 *
 * Rather than re-port canvas.py's front-end (and lose features to OWUI's CSP),
 * this embeds the REAL canvas page as a full-bleed iframe behind OWUI. You get
 * everything with zero drift: image cross-fade + Ken Burns, speech-reactive
 * motion, beats, the tap-to-reveal audio controller, the WebAudio cues, the
 * muted YouTube music mirror, wake-lock, and e-ink mode.
 *
 * Why the iframe wins here:
 *   - The canvas's external deps (the YouTube IFrame API + embed) load inside
 *     the iframe's OWN document, governed by canvas.py's response — NOT OWUI's
 *     policy. So the video mirror works without punching youtube into OWUI.
 *   - Served same-origin (Caddy proxies /canvas/ + the canvas's root endpoints
 *     → :8781), the iframe, its SSE, and its images are all first-party. No
 *     CORS, no mixed origin, and the parent can poke the iframe (below).
 *   - It IS canvas.py's page, so it can never fall out of sync with the wall
 *     canvas. Nothing to maintain twice.
 *
 * The one tradeoff: an iframe's pointer events are all-or-nothing, so the
 * canvas is click-through wallpaper by default and a small floating button
 * brings it FORWARD (interactive) when you want the audio controller, then
 * sends it back. That first forward-tap also satisfies the browser's autoplay
 * gate, unlocking the WebAudio cues inside the iframe.
 *
 * IMPORTANT (de-risked against current main): canvas.js requests its endpoints
 * with ORIGIN-ABSOLUTE paths — new EventSource('/events'), <img src="/img/…">,
 * fetch('/ctl'), … — NOT relative to /canvas. So a bare `/canvas/*` prefix
 * proxy loads the iframe page but its stream/images then escape the prefix and
 * hit OWUI. The shipped Caddyfile therefore ALSO proxies the canvas's own root
 * endpoints (/events, /img/*, /ctl, …) to :8781. Keep the two in lockstep. See
 * packages/visual/deploy/owui-canvas.Caddyfile + deploy/README.md.
 */
(function () {
  'use strict';

  const qp = new URLSearchParams(location.search);
  const CFG = {
    // e-ink: white page, no motion/video. Auto-on if the OWUI URL carries
    // ?eink=1; force it here for a device that's always e-ink (e.g. PineNote).
    EINK: qp.get('eink') === '1',
    // scrim behind OWUI's chat so bubbles read over the artwork
    SCRIM: true,
  };
  // Where the canvas is reachable from OWUI's origin. Default is the same-origin
  // '/canvas' mount the shipped Caddyfile serves. Override without editing this
  // file for the alternative deploy shapes (see deploy/README.md):
  //   - ?canvasbase=<v> on the OWUI URL  (also how the headless harness points
  //     it at a throwaway server: ?canvasbase= → '' → iframe src '/')
  //   - window.AMC_BASE = 'https://canvas.example.ts.net'  (canvas on its own
  //     hostname; the parent→iframe poke below no-ops cross-origin, harmless)
  const BASE = qp.has('canvasbase') ? qp.get('canvasbase')
             : (typeof window.AMC_BASE === 'string' ? window.AMC_BASE : '/canvas');
  const Z_BACK = '-1';
  const Z_FRONT = '2147483000';

  // ---- the canvas, as a full-viewport iframe behind OWUI -------------------
  const frame = document.createElement('iframe');
  frame.id = 'amc-frame';
  frame.src = BASE + '/' + (CFG.EINK ? '?eink=1' : '');
  frame.setAttribute('title', 'agent-media canvas');
  // allow the wake-lock + autoplay the canvas asks for
  frame.setAttribute('allow', 'autoplay; screen-wake-lock');

  // ---- a small handle to toggle interactivity (for the controller) --------
  const fab = document.createElement('button');
  fab.id = 'amc-fab';
  fab.type = 'button';
  fab.title = 'canvas controls';
  fab.textContent = '▣';

  const css = document.createElement('style');
  css.textContent = `
  #amc-frame {
    position: fixed; inset: 0; width: 100vw; height: 100vh; border: 0;
    background: ${CFG.EINK ? '#fff' : '#000'};
    z-index: ${Z_BACK}; pointer-events: none;   /* click-through by default */
  }
  #amc-frame.amc-front { z-index: ${Z_FRONT}; pointer-events: auto; }
  #amc-fab {
    position: fixed; z-index: 2147483001;
    right: max(12px, env(safe-area-inset-right));
    bottom: max(12px, env(safe-area-inset-bottom));
    width: 42px; height: 42px; border-radius: 50%; border: 0;
    font: 18px/1 system-ui, sans-serif; cursor: pointer;
    color: #eee; background: rgba(10,10,10,.55); backdrop-filter: blur(10px);
    opacity: .5; transition: opacity .25s ease; }
  #amc-fab:hover, #amc-fab.amc-on { opacity: 1; }
  #amc-fab.amc-on { color: #ffd75f; background: rgba(10,10,10,.8); }
  `;

  // ---- OWUI transparency override ------------------------------------------
  // The one version-BRITTLE bit: OWUI must be see-through for the canvas to
  // show. These selectors target OWUI's SvelteKit shell — re-check them when
  // you bump the pinned OWUI version (current: v0.10.2).
  const owui = document.createElement('style');
  owui.textContent = `
  html, body, #app { background: transparent !important; }
  ${CFG.SCRIM ? `
  /* scrim behind the chat so bubbles read over the artwork; widen/adjust the
     selector to whatever wraps OWUI's message list in your version. */
  main, .chat-container, [class*="messages"] {
    background: ${CFG.EINK ? 'rgba(255,255,255,.9)' : 'rgba(12,12,14,.62)'} !important;
    backdrop-filter: blur(6px); }` : ''}
  `;

  function toggleFront() {
    const front = frame.classList.toggle('amc-front');
    fab.classList.toggle('amc-on', front);
    // When we bring it forward, poke the iframe so its controller reveals and
    // its AudioContext unlocks on this genuine user gesture. Same-origin only;
    // a cross-origin BASE (canvas on its own host) makes this a no-op — the
    // user's own tap on the controller then does the unlocking instead.
    if (front) {
      try { frame.contentWindow.focus(); } catch (_) {}
      try {
        // canvas.py reveals its controller on a tap anywhere on its body
        frame.contentWindow.document.body.dispatchEvent(
          new PointerEvent('pointerdown', { bubbles: true }));
      } catch (_) { /* cross-doc guard; harmless if it no-ops */ }
    }
  }

  function boot() {
    document.head.appendChild(css);
    document.head.appendChild(owui);
    document.body.appendChild(frame);
    document.body.appendChild(fab);
    fab.addEventListener('click', toggleFront);
    // Esc sends the canvas back when it's forward
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && frame.classList.contains('amc-front')) toggleFront();
    });
  }

  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
