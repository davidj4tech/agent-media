// Headless verification for bg.js — the OWUI background loader (deploy/bg.js).
//
// bg.js runs inside OWUI's page and embeds canvas.py's real page as an iframe
// behind the chat. This rig serves a MOCK OWUI shell same-origin with a
// throwaway canvas, loads bg.js into it with ?canvasbase= (so the iframe points
// at the throwaway '/'), and asserts the behaviours a deploy can't eyeball in
// CI: the iframe loads the real canvas, the ▣ FAB flips the canvas
// front↔back (pointer-events + z-index), Esc sends it back, the OWUI
// transparency + chat scrim apply, and the ?eink=1 path is white with the
// canvas in e-ink mode.
//
// Never touches the live wall service — its own throwaway canvas on 127.0.0.1,
// and /input + /ctl are route-blocked so no keystroke/CLI can escape (the
// canvas-headless-harness rule).
'use strict';
const { spawn } = require('child_process');
const http = require('http');
const net = require('net');
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const REPO = path.resolve(__dirname, '..', '..', '..', '..');
const SRC = process.env.MEDIA_HARNESS_SRC || path.join(REPO, 'packages', 'visual', 'src');
const PY = path.join(REPO, '.venv', 'bin', 'python');
const SRV_PORT = Number(process.env.MEDIA_BG_PORT || 8793);
const PROXY_PORT = Number(process.env.MEDIA_BG_PROXY_PORT || 8794);
const SHOTS = path.join(__dirname, 'shots');
fs.mkdirSync(SHOTS, { recursive: true });

const BG_JS = fs.readFileSync(
  path.join(REPO, 'packages', 'visual', 'static', 'bg.js'), 'utf8');

// A stand-in for OWUI's SvelteKit shell: the selectors bg.js targets
// (html/body/#app transparency, the message-list scrim) so we can prove the
// override lands on something OWUI-shaped.
const MOCK_OWUI = `<!doctype html><html><head><title>mock owui</title>
<style>html,body{margin:0;background:#123}#app{background:#123}main{background:#222}</style>
</head><body><div id="app"><main class="chat-container"><div class="messages">
<div class="message">a chat bubble</div></div></main></div>
<script>${BG_JS}</script></body></html>`;

const results = [];
function rec(name, pass, detail) {
  results.push({ name, pass, detail });
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ---- throwaway canvas server (same bootstrap as harness.js) ----------------
let srv = null;
function startServer() {
  srv = spawn(PY, ['-m', 'agent_media_visual.canvas'], {
    cwd: REPO,
    env: {
      ...process.env,
      PYTHONPATH: SRC,                 // beats the venv editable install
      MEDIA_VISUAL_BIND: '127.0.0.1',
      MEDIA_VISUAL_PORT: String(SRV_PORT),
      MEDIA_VISUAL_TRUST_TAILNET: '1',
      MEDIA_VISUAL_VIDEO: '0',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  srv.stdout.on('data', d => process.stdout.write('[srv] ' + d));
  srv.stderr.on('data', d => process.stdout.write('[srv!] ' + d));
}
function httpGet(port, p, timeoutMs = 4000) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, path: p, timeout: timeoutMs }, (res) => {
      let body = ''; res.on('data', c => body += c);
      res.on('end', () => resolve({ status: res.statusCode, body }));
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
  });
}
async function waitServer(budget = 15000) {
  const t0 = Date.now();
  while (Date.now() - t0 < budget) {
    const r = await httpGet(SRV_PORT, '/healthz', 1500);
    if (r && r.status === 200) return true;
    await sleep(400);
  }
  return false;
}

// ---- plain pass-through proxy: browser :PROXY_PORT -> canvas :SRV_PORT ------
// (bg.js's iframe + the canvas's root endpoints all ride this one origin.)
const pipes = new Set();
const proxy = net.createServer((c) => {
  const b = net.connect(SRV_PORT, '127.0.0.1');
  const pair = { c, b }; pipes.add(pair);
  c.on('error', () => {});
  b.on('error', () => { c.destroy(); pipes.delete(pair); });
  c.pipe(b); b.pipe(c);
  const clean = () => { pipes.delete(pair); c.destroy(); b.destroy(); };
  c.on('close', clean); b.on('close', clean);
});

function canvasFrame(page) {
  return page.frames().find(f => f !== page.mainFrame());
}

(async () => {
  startServer();
  if (!await waitServer()) { console.error('canvas server never came up'); process.exit(1); }
  await new Promise(r => proxy.listen(PROXY_PORT, '127.0.0.1', r));
  console.log('rig up: canvas :' + SRV_PORT + ', proxy :' + PROXY_PORT + ', src ' + SRC);

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });

  // Serve the mock OWUI shell same-origin as the canvas; everything else
  // (the iframe's '/', /events, /img, /status, …) passes through to the proxy.
  await page.route('**/owui-mock*', (route) =>
    route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: MOCK_OWUI }));
  // Belt + braces: keystrokes/CLI must never escape a test.
  await page.route('**/input', (route) => route.abort());
  await page.route('**/ctl', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' }));

  const cs = (sel, prop) => page.evaluate(([s, p]) => {
    const el = document.querySelector(s); if (!el) return null;
    return getComputedStyle(el)[p];
  }, [sel, prop]);

  // ================= normal (non-eink) shell ================================
  await page.goto(`http://127.0.0.1:${PROXY_PORT}/owui-mock?canvasbase=`, { waitUntil: 'domcontentloaded' });

  // T1: the loader builds its iframe + FAB, iframe points at the canvas root.
  let t1 = false, srcOk = false, allowOk = false;
  try {
    await page.waitForSelector('#amc-frame', { timeout: 5000 });
    await page.waitForSelector('#amc-fab', { timeout: 5000 });
    const info = await page.evaluate(() => {
      const f = document.getElementById('amc-frame');
      return { src: f.getAttribute('src'), allow: f.getAttribute('allow') };
    });
    t1 = true;
    srcOk = info.src === '/';                         // canvasbase='' -> ''+'/'
    allowOk = /autoplay/.test(info.allow) && /screen-wake-lock/.test(info.allow);
  } catch {}
  rec('bgT1 loader builds iframe + FAB', t1);
  rec('bgT1b iframe src is the canvas root, autoplay/wake-lock allowed',
    srcOk && allowOk, `src=${srcOk} allow=${allowOk}`);

  // T2: click-through wallpaper by default — behind, no pointer events.
  {
    const pe = await cs('#amc-frame', 'pointerEvents');
    const z = await cs('#amc-frame', 'zIndex');
    rec('bgT2 canvas is behind + click-through by default',
      pe === 'none' && z === '-1', `pointer-events=${pe} z-index=${z}`);
  }

  // T3: the iframe actually loaded the REAL canvas (same-origin: reach inside).
  {
    let dotSeen = false;
    try {
      const cf = canvasFrame(page);
      if (cf) { await cf.waitForSelector('#dot', { timeout: 10000 }); dotSeen = true; }
    } catch {}
    rec('bgT3 iframe loads the real canvas page (#dot present)', dotSeen);
  }

  // T4: OWUI transparency + chat scrim applied.
  {
    const htmlBg = await cs('html', 'backgroundColor');
    const bodyBg = await cs('body', 'backgroundColor');
    const mainBg = await cs('main', 'backgroundColor');
    const transparent = (v) => v === 'rgba(0, 0, 0, 0)' || v === 'transparent';
    const scrimOn = /rgba\(12, 12, 14/.test(mainBg);   // the dark scrim
    rec('bgT4 OWUI shell made transparent (html/body)',
      transparent(htmlBg) && transparent(bodyBg), `html=${htmlBg} body=${bodyBg}`);
    rec('bgT4b chat message-list gets the artwork scrim', scrimOn, `main=${mainBg}`);
  }

  // T5: the FAB toggles the canvas front↔back. The FAB (z above the frame)
  // is the reliable toggle both ways. Note: bringing it forward focuses the
  // iframe, so a subsequent Esc goes to the CANVAS (its own ring), not to
  // bg.js — that's why re-clicking the FAB, not Esc, is the primary back-path.
  {
    await page.click('#amc-fab'); await sleep(200);
    const front = await page.evaluate(() => ({
      cls: document.getElementById('amc-frame').classList.contains('amc-front'),
      pe: getComputedStyle(document.getElementById('amc-frame')).pointerEvents,
      z: getComputedStyle(document.getElementById('amc-frame')).zIndex,
      fabOn: document.getElementById('amc-fab').classList.contains('amc-on'),
    }));
    rec('bgT5 FAB brings canvas forward (interactive, on top)',
      front.cls && front.pe === 'auto' && front.z === '2147483000' && front.fabOn,
      JSON.stringify(front));
    await page.screenshot({ path: SHOTS + '/10-bg-front.png' });
    await page.click('#amc-fab'); await sleep(200);
    const back = await page.evaluate(() => ({
      cls: document.getElementById('amc-frame').classList.contains('amc-front'),
      pe: getComputedStyle(document.getElementById('amc-frame')).pointerEvents,
      fabOn: document.getElementById('amc-fab').classList.contains('amc-on'),
    }));
    rec('bgT5b FAB re-click sends the canvas back (click-through again)',
      !back.cls && back.pe === 'none' && !back.fabOn, JSON.stringify(back));
    await page.screenshot({ path: SHOTS + '/11-bg-back.png' });
  }

  // T5c: Esc is the secondary back-path — it only works while the PARENT (OWUI)
  // holds focus (the canvas swallows Esc otherwise). Forward, park focus on the
  // FAB (a parent element), then Esc must send it back.
  {
    await page.click('#amc-fab'); await sleep(150);
    await page.focus('#amc-fab');               // pull focus back out of the iframe
    await page.keyboard.press('Escape'); await sleep(200);
    const back = await page.evaluate(() =>
      !document.getElementById('amc-frame').classList.contains('amc-front'));
    rec('bgT5c Esc sends it back when OWUI holds focus', back);
  }

  // ================= e-ink shell (?eink=1) =================================
  {
    await page.goto(`http://127.0.0.1:${PROXY_PORT}/owui-mock?canvasbase=&eink=1`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#amc-frame', { timeout: 5000 });
    const info = await page.evaluate(() => {
      const f = document.getElementById('amc-frame');
      return { src: f.getAttribute('src'), bg: getComputedStyle(f).backgroundColor };
    });
    const srcOk = info.src === '/?eink=1';
    const whiteFrame = info.bg === 'rgb(255, 255, 255)';
    // canvas inside must go DU4 e-ink (no motion/video) — <html class="eink">
    let einkInside = false;
    try {
      const cf = canvasFrame(page);
      if (cf) {
        await cf.waitForSelector('#dot', { timeout: 10000 });
        einkInside = await cf.evaluate(() => document.documentElement.classList.contains('eink'));
      }
    } catch {}
    rec('bgT6 ?eink=1 → white iframe, e-ink passed through to the canvas',
      srcOk && whiteFrame && einkInside, `src=${srcOk} white=${whiteFrame} canvasEink=${einkInside}`);
    // scrim flips to near-opaque white so bubbles stay legible on the panel
    const mainBg = await page.evaluate(() => getComputedStyle(document.querySelector('main')).backgroundColor);
    rec('bgT6b e-ink scrim is near-opaque white', /rgba\(255, 255, 255/.test(mainBg), `main=${mainBg}`);
    await page.screenshot({ path: SHOTS + '/12-bg-eink.png' });
  }

  await browser.close();
  proxy.close();
  if (srv) srv.kill('SIGKILL');
  const fails = results.filter(r => !r.pass);
  fs.writeFileSync(path.join(__dirname, 'bg-results.json'), JSON.stringify(results, null, 2));
  console.log(`\n==== ${results.length - fails.length}/${results.length} passed ====`);
  process.exit(fails.length ? 2 : 0);
})().catch(e => { console.error('BG HARNESS ERROR', e); if (srv) srv.kill('SIGKILL'); process.exit(3); });
