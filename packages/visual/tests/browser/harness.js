// Headless verification harness for the canvas client JS (#137 #141 #142 #146).
// Runs a throwaway canvas instance on 127.0.0.1, fronts it with a stallable TCP
// proxy (the only way to reproduce a *silently* stalled SSE stream), and drives
// the real client JS with Playwright chromium. Never touches the live wall
// service. See README.md in this directory for setup and safety notes.
//
// Point MEDIA_HARNESS_SRC at a worktree's packages/visual/src to test a branch
// without reinstalling; defaults to this repo's source tree.
'use strict';
const { spawn } = require('child_process');
const net = require('net');
const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const REPO = path.resolve(__dirname, '..', '..', '..', '..');
const SRC = process.env.MEDIA_HARNESS_SRC || path.join(REPO, 'packages', 'visual', 'src');
const PY = path.join(REPO, '.venv', 'bin', 'python');
const SRV_PORT = Number(process.env.MEDIA_HARNESS_PORT || 8791);
const PROXY_PORT = Number(process.env.MEDIA_HARNESS_PROXY_PORT || 8792);
const SHOTS = path.join(__dirname, 'shots');
fs.mkdirSync(SHOTS, { recursive: true });

const results = [];
function rec(name, pass, detail) {
  results.push({ name, pass, detail });
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ---- throwaway server ------------------------------------------------------
let srv = null;
function startServer() {
  srv = spawn(PY, ['-m', 'agent_media_visual.canvas'], {
    cwd: REPO,
    env: {
      ...process.env,
      // PYTHONPATH beats the venv's editable install, so SRC can be any branch.
      PYTHONPATH: SRC,
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
      let body = '';
      res.on('data', c => body += c);
      res.on('end', () => resolve({ status: res.statusCode, body }));
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
  });
}
async function waitServer(up = true, budget = 15000) {
  const t0 = Date.now();
  while (Date.now() - t0 < budget) {
    const r = await httpGet(SRV_PORT, '/status', 1500);
    if (up ? r && r.status === 200 : !r) return true;
    await sleep(400);
  }
  return false;
}

// ---- stallable proxy: browser :PROXY_PORT -> server :SRV_PORT ---------------
let stalled = false;
const pipes = new Set();
const proxy = net.createServer((c) => {
  const b = net.connect(SRV_PORT, '127.0.0.1');
  const pair = { c, b };
  pipes.add(pair);
  c.on('error', () => {});
  b.on('error', () => { c.destroy(); pipes.delete(pair); });
  c.pipe(b); b.pipe(c);
  if (stalled) { c.pause(); b.pause(); }
  const clean = () => { pipes.delete(pair); c.destroy(); b.destroy(); };
  c.on('close', clean); b.on('close', clean);
});
function stall(on) {
  stalled = on;
  for (const { c, b } of pipes) { if (on) { c.pause(); b.pause(); } else { c.resume(); b.resume(); } }
}

(async () => {
  startServer();
  if (!await waitServer(true)) { console.error('server never came up'); process.exit(1); }
  await new Promise(r => proxy.listen(PROXY_PORT, '127.0.0.1', r));
  console.log('rig up: server :' + SRV_PORT + ', proxy :' + PROXY_PORT + ', src ' + SRC);

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });

  // Instrument EventSource + native prompt before any page JS runs.
  await page.addInitScript(() => {
    window.__esCount = 0; window.__pings = 0; window.__msgs = 0; window.__promptCalled = 0;
    const Orig = window.EventSource;
    window.EventSource = function (url, opts) {
      window.__esCount++;
      const es = new Orig(url, opts);
      es.addEventListener('message', (e) => {
        window.__msgs++;
        try { if (JSON.parse(e.data).kind === 'ping') window.__pings++; } catch (_) {}
      });
      return es;
    };
    window.EventSource.prototype = Orig.prototype;
    window.prompt = () => { window.__promptCalled++; return null; };
  });

  // Intercept /input: never let keystrokes reach real tmux panes.
  let inputMode = 'block';
  const inputReqs = [];
  await page.route('**/input', (route) => {
    inputReqs.push({ headers: route.request().headers(), post: route.request().postData() });
    if (inputMode === '401') return route.fulfill({ status: 401, contentType: 'application/json', body: '{}' });
    if (inputMode === 'ok') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' });
    if (inputMode === 'errdetail') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: false, detail: 'no such pane: %99' }) });
    return route.abort();
  });

  // Intercept /ctl too: controller taps must never drive the real `media` CLI
  // (pause the house audio, flip the shared popup-channel state) from a test.
  await page.route('**/ctl', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' }));

  // Count /agents polls from the page.
  const agentsReqs = [];
  page.on('request', (r) => { if (r.url().endsWith('/agents')) agentsReqs.push(Date.now()); });
  const pollsIn = (t0, t1) => agentsReqs.filter(t => t >= t0 && t <= t1).length;

  // Track /status polls (T16 reads the channel param to observe `n` cycling).
  const statusReqs = [];
  page.on('request', (r) => {
    const m = r.url().match(/\/status\?channel=(\w+)/);
    if (m) statusReqs.push(m[1]);
  });

  // ---- T1: load + SSE connect ----------------------------------------------
  await page.goto(`http://127.0.0.1:${PROXY_PORT}/`, { waitUntil: 'domcontentloaded' });
  try {
    await page.waitForFunction(() => !document.getElementById('dot').classList.contains('off'), null, { timeout: 10000 });
    rec('T1 SSE connects (dot on)', true, `esCount=${await page.evaluate(() => window.__esCount)}`);
  } catch { rec('T1 SSE connects (dot on)', false, 'dot stayed .off'); }
  await page.screenshot({ path: SHOTS + '/01-baseline.png' });

  // ---- T2: heartbeat / liveness frames (#137 server half) -------------------
  // The ping only fires after 15s of a *quiet* event queue; when the house is
  // actively speaking, state frames flow instead — either kind stamps
  // lastEventTs and feeds the watchdog, which is the actual requirement.
  {
    const m0 = await page.evaluate(() => window.__msgs);
    let pings = 0, m1 = m0;
    const t0 = Date.now();
    while (Date.now() - t0 < 40000) {
      await sleep(2000);
      pings = await page.evaluate(() => window.__pings);
      m1 = await page.evaluate(() => window.__msgs);
      if (pings >= 1) break;
    }
    rec('T2 SSE liveness frames stamp watchdog', pings >= 1 || m1 > m0,
      pings >= 1 ? `ping heartbeat seen (pings=${pings})`
                 : `no idle window for a ping, but ${m1 - m0} state frames flowed (watchdog fed)`);
  }

  // ---- T3: server memoizes /agents ~2s (#141 server half) -------------------
  {
    const a = await httpGet(SRV_PORT, '/agents', 8000);
    const b = await httpGet(SRV_PORT, '/agents', 8000);
    const memoOk = a && b && a.status === 200 && a.body === b.body;
    await sleep(2500);
    const c = await httpGet(SRV_PORT, '/agents', 8000);
    rec('T3 /agents memoized within 2s', !!memoOk, memoOk ? `identical bodies; post-TTL refetch ${c ? c.status : 'n/a'}` : 'bodies differ or error');
  }

  // ---- T4: collapsed cadence ~12s (#141) ------------------------------------
  {
    const t0 = Date.now();
    await sleep(26000);
    const n = pollsIn(t0, Date.now());
    rec('T4 collapsed poll cadence ~12s', n >= 1 && n <= 3, `${n} polls in 26s (expect ~2)`);
  }

  // ---- T5: expanded cadence ~4s (#141) --------------------------------------
  {
    // The tree re-renders under the pill on every poll, so never hold an
    // element handle across a poll window — query fresh for each click.
    const clickPill = () => page.click('.aghead', { timeout: 5000 }).then(() => true).catch(() => false);
    if (!await page.$('.aghead')) rec('T5 expanded poll cadence ~4s', false, 'no agents pill rendered — no tmux claude panes visible to throwaway instance?');
    else {
      await clickPill();
      const t0 = Date.now();
      await sleep(13500);
      const n = pollsIn(t0, Date.now());
      rec('T5 expanded poll cadence ~4s', n >= 2 && n <= 5, `${n} polls in 13.5s (expect ~3)`);
      if (!await clickPill()) await clickPill();   // collapse again (retry once past a re-render)
    }
  }

  // ---- T6: hidden gating (#141) ---------------------------------------------
  {
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    const t0 = Date.now();
    await sleep(35000);
    const nHidden = pollsIn(t0, Date.now());
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await sleep(2000);
    const nBack = pollsIn(t0 + 35000, Date.now());
    rec('T6 hidden gates /agents polls', nHidden <= 1 && nBack >= 1, `hidden 35s: ${nHidden} polls (expect 0-1); visible again: ${nBack} immediate poll(s)`);
  }

  // ---- T7: 401 -> pairing toast + in-page token sheet (#142) ----------------
  {
    inputMode = '401';
    const before = inputReqs.length;
    await page.evaluate(() => {
      document.getElementById('text').value = 'harness test';
      document.getElementById('send').click();
    });
    let toastOk = false, sheetOk = false;
    try {
      await page.waitForFunction(() =>
        document.getElementById('sheet').classList.contains('on') &&
        document.getElementById('sheettitle').textContent.includes('amux auth token'), null, { timeout: 5000 });
      sheetOk = true;
      toastOk = await page.evaluate(() => document.getElementById('toast').textContent.includes('not paired'));
    } catch {}
    await page.screenshot({ path: SHOTS + '/02-token-sheet.png' });
    rec('T7a 401 -> pairing toast + token sheet', toastOk && sheetOk, `sheet=${sheetOk} toast=${toastOk}`);
    // Enter a token; the retry must carry it.
    inputMode = 'ok';
    await page.fill('#sheetin', 'tok-harness-123');
    await page.press('#sheetin', 'Enter');
    await sleep(1500);
    const retry = inputReqs[inputReqs.length - 1];
    const carried = inputReqs.length >= before + 2 && retry.headers['x-auth-token'] === 'tok-harness-123';
    const stored = await page.evaluate(() => localStorage.getItem('amux_token'));
    const sent = await page.evaluate(() => document.getElementById('send').textContent);
    rec('T7b sheet token retries request', carried && stored === 'tok-harness-123',
      `retry header=${retry && retry.headers['x-auth-token']} stored=${stored} send=${JSON.stringify(sent)}`);
  }

  // ---- T8: sheet Esc cancels, no retry (#142) -------------------------------
  {
    await page.evaluate(() => localStorage.removeItem('amux_token'));
    inputMode = '401';
    const before = inputReqs.length;
    await page.evaluate(() => {
      document.getElementById('text').value = 'esc test';
      document.getElementById('send').click();
    });
    let opened = false;
    try {
      await page.waitForFunction(() => document.getElementById('sheet').classList.contains('on'), null, { timeout: 5000 });
      opened = true;
    } catch {}
    await page.press('#sheetin', 'Escape');
    await sleep(1200);
    const closed = await page.evaluate(() => !document.getElementById('sheet').classList.contains('on'));
    const noRetry = inputReqs.length === before + 1;
    rec('T8 Esc cancels sheet, no retry', opened && closed && noRetry, `opened=${opened} closed=${closed} reqs=${inputReqs.length - before}`);
  }

  // ---- T9: /input error detail -> toast (#142) ------------------------------
  {
    await page.evaluate(() => localStorage.setItem('amux_token', 'tok-harness-123'));
    inputMode = 'errdetail';
    await page.evaluate(() => {
      document.getElementById('text').value = 'err test';
      document.getElementById('send').click();
    });
    let ok = false;
    try {
      await page.waitForFunction(() => document.getElementById('toast').textContent.includes('no such pane'), null, { timeout: 5000 });
      ok = true;
    } catch {}
    rec('T9 /input error detail shown as toast', ok);
  }

  // ---- T10: kill server -> offbar + dim; restart -> self-heal ---------------
  {
    srv.kill('SIGKILL');
    await waitServer(false, 8000);
    let dotOff = false, barOn = false;
    try {
      await page.waitForFunction(() => document.getElementById('dot').classList.contains('off'), null, { timeout: 8000 });
      dotOff = true;
      await page.waitForFunction(() => document.getElementById('offbar').classList.contains('on'), null, { timeout: 15000 });
      barOn = true;
    } catch {}
    await page.screenshot({ path: SHOTS + '/03-offbar.png' });
    rec('T10a server dead -> dot off + reconnect banner', dotOff && barOn, `dot=${dotOff} offbar=${barOn}`);
    startServer();
    await waitServer(true);
    let healed = false;
    try {
      await page.waitForFunction(() =>
        !document.getElementById('offbar').classList.contains('on') &&
        !document.getElementById('dot').classList.contains('off'), null, { timeout: 25000 });
      healed = true;
    } catch {}
    await page.screenshot({ path: SHOTS + '/04-healed.png' });
    rec('T10b restart -> banner clears, reconnected', healed);
  }

  // ---- T11: silent stall -> watchdog reconnect (#137 client half) -----------
  {
    const esBefore = await page.evaluate(() => window.__esCount);
    stall(true);
    let watchdogFired = false, barOn = false;
    const t0 = Date.now();
    while (Date.now() - t0 < 80000) {
      await sleep(3000);
      const es = await page.evaluate(() => window.__esCount);
      barOn = await page.evaluate(() => document.getElementById('offbar').classList.contains('on'));
      if (es > esBefore && barOn) { watchdogFired = true; break; }
    }
    await page.screenshot({ path: SHOTS + '/05-stalled.png' });
    rec('T11a stalled stream -> watchdog reconnects + banner', watchdogFired,
      `esCount ${esBefore} -> ${await page.evaluate(() => window.__esCount)} after ${Math.round((Date.now() - t0) / 1000)}s, offbar=${barOn}`);
    stall(false);
    let healed = false;
    try {
      await page.waitForFunction(() =>
        !document.getElementById('offbar').classList.contains('on') &&
        !document.getElementById('dot').classList.contains('off'), null, { timeout: 35000 });
      healed = true;
    } catch {}
    await page.screenshot({ path: SHOTS + '/06-unstalled.png' });
    rec('T11b unstall -> stream heals, banner clears', healed);
  }

  // ---- T12: MAX_SSE_CLIENTS cap -> 503 (#137 server half) --------------------
  {
    const held = [];
    let got503 = false, accepted = 0;
    for (let i = 0; i < 70 && !got503; i++) {
      const status = await new Promise((resolve) => {
        const req = http.get({ host: '127.0.0.1', port: SRV_PORT, path: '/events' }, (res) => {
          held.push(req); resolve(res.statusCode);
        });
        req.on('error', () => resolve(0));
      });
      if (status === 503) got503 = true;
      else if (status === 200) accepted++;
      else break;
    }
    for (const r of held) r.destroy();
    rec('T12 SSE client cap sheds load (503)', got503 && accepted <= 64, `accepted ${accepted} extra streams before 503 (page holds ~1; cap 64)`);
  }

  // ---- T13: native prompt never used (#142) ---------------------------------
  rec('T13 native prompt() never called', (await page.evaluate(() => window.__promptCalled)) === 0);

  // ---- T14/T15: e-ink theme + solid toast (#146) -----------------------------
  {
    await page.goto(`http://127.0.0.1:${PROXY_PORT}/?eink=1`, { waitUntil: 'domcontentloaded' });
    await sleep(1500);
    const einkOn = await page.evaluate(() => document.documentElement.classList.contains('eink'));
    await page.screenshot({ path: SHOTS + '/07-eink.png' });
    inputMode = '401';
    await page.evaluate(() => {
      localStorage.removeItem('amux_token');
      document.getElementById('text').value = 'eink sheet';
      document.getElementById('send').click();
    });
    try {
      await page.waitForFunction(() => document.getElementById('sheet').classList.contains('on'), null, { timeout: 5000 });
    } catch {}
    await page.screenshot({ path: SHOTS + '/08-eink-sheet.png' });
    rec('T14 e-ink theme applies (screenshots for eyeballing)', einkOn);
    // Toast must be a solid black-on-white pill in DU4 — no alpha, blur, shadow.
    const ts = await page.evaluate(() => {
      const s = getComputedStyle(document.getElementById('toast'));
      return { bg: s.backgroundColor, color: s.color, bw: s.borderTopWidth, blur: s.backdropFilter, shadow: s.boxShadow };
    });
    const toastSolid = ts.bg === 'rgb(255, 255, 255)' && ts.color === 'rgb(0, 0, 0)' &&
      ts.bw === '2px' && ts.blur === 'none' && ts.shadow === 'none';
    rec('T15 e-ink toast is solid + legible', toastSolid, JSON.stringify(ts));
  }

  // ---- T16: focus ring — taps and Tab walk passive→input→agents→control→… ---
  // The same ring for both surfaces; `n` (not Tab) cycles the channel in
  // CONTROL; Esc bails out; widget taps never advance the ring.
  {
    inputMode = 'block';
    await page.evaluate(() => localStorage.setItem('eink', '0'));
    await page.goto(`http://127.0.0.1:${PROXY_PORT}/`, { waitUntil: 'domcontentloaded' });
    await sleep(1500);   // let the first /agents poll land
    const state = () => page.evaluate(() => ({
      input: document.getElementById('inp').classList.contains('on'),
      ctl: document.getElementById('ctl').classList.contains('on'),
      agents: document.getElementById('agents').classList.contains('on'),
      agExpanded: document.getElementById('agents').classList.contains('expanded'),
      agCursor: !!document.getElementById('agents').querySelector('.cursor'),
      typing: document.activeElement === document.getElementById('text'),
    }));
    const tap = () => page.mouse.click(640, 300);   // bare canvas, away from widgets
    const fmt = (s) => JSON.stringify(s);

    let s = await state();
    const hasTree = s.agents;
    rec('T16a page starts passive', !s.input && !s.ctl, `tree=${hasTree}`);

    await tap(); await sleep(300); s = await state();
    rec('T16b tap: passive -> input (field focused)', s.input && s.typing, fmt(s));

    await tap(); await sleep(300); s = await state();
    if (hasTree)
      rec('T16c tap: input -> agents (tree cursor)', !s.input && !s.ctl && s.agExpanded && s.agCursor, fmt(s));
    else
      rec('T16c tap: input -> control (no tree)', !s.input && s.ctl, fmt(s));
    if (hasTree) { await tap(); await sleep(300); s = await state(); }
    rec('T16d ring reaches control', s.ctl && !s.agCursor, fmt(s));

    const chanBefore = statusReqs[statusReqs.length - 1];
    await page.keyboard.press('n'); await sleep(400); s = await state();
    const chanAfter = statusReqs[statusReqs.length - 1];
    rec("T16e 'n' in control cycles channel", s.ctl && chanAfter && chanAfter !== chanBefore,
      `'${chanBefore}' -> '${chanAfter}'`);

    await tap(); await sleep(300); s = await state();
    rec('T16f tap in control wraps to passive', !s.input && !s.ctl, fmt(s));

    for (let i = 0; i < (hasTree ? 3 : 2); i++) { await page.keyboard.press('Tab'); await sleep(250); }
    s = await state();
    rec('T16g Tab walks the same ring to control', s.ctl, fmt(s));
    await page.keyboard.press('Escape'); await sleep(300); s = await state();
    rec('T16h Esc bails out to passive', !s.input && !s.ctl, fmt(s));

    for (let i = 0; i < (hasTree ? 3 : 2); i++) { await tap(); await sleep(250); }
    s = await state();
    if (s.ctl) {
      await page.click('#pp'); await sleep(300); s = await state();
      rec('T16i controller button tap stays in control', s.ctl, fmt(s));
      await page.click('#xbtn'); await sleep(300); s = await state();
      rec('T16j x button drops to passive', !s.ctl && !s.input, fmt(s));
    } else rec('T16i/j reached control for widget-tap checks', false, fmt(s));
    await page.screenshot({ path: SHOTS + '/09-ring.png' });
  }

  await browser.close();
  proxy.close();
  if (srv) srv.kill('SIGKILL');
  const fails = results.filter(r => !r.pass);
  fs.writeFileSync(path.join(__dirname, 'results.json'), JSON.stringify(results, null, 2));
  console.log(`\n==== ${results.length - fails.length}/${results.length} passed ====`);
  process.exit(fails.length ? 2 : 0);
})().catch(e => { console.error('HARNESS ERROR', e); if (srv) srv.kill('SIGKILL'); process.exit(3); });
