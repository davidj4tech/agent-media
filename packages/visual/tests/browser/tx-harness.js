// Transcript harness: the subtitle band opens out into the whole clip, the
// live sentence lit, and what the voice has not reached yet is masked until
// the reader deliberately scrolls past it. Also covers pinch — reading size on
// the words, zoom on the picture. 10 checks, ~20s. Screenshots in shots/.
//
// Same shape as band-harness.js and for the same reason: /events is aborted so
// a genuine speaking:false frame from the real house state cannot clear the
// injected clip mid-measurement, and /input is aborted so nothing can type
// into live tmux.
'use strict';
const { spawn } = require('child_process');
const http = require('http');
const path = require('path');
const fs = require('fs');
const { chromium } = require('playwright');

const REPO = '/home/ryer/projects/agent-media';
const SRC = path.join(REPO, 'packages', 'visual', 'src');
const PY = path.join(REPO, '.venv', 'bin', 'python');
const PORT = Number(process.env.MEDIA_TX_PORT || 8794);
const SHOTS = path.join(__dirname, 'shots');
fs.mkdirSync(SHOTS, { recursive: true });
const sleep = ms => new Promise(r => setTimeout(r, ms));

const srv = spawn(PY, ['-m', 'agent_media_visual.canvas'], {
  cwd: REPO,
  env: { ...process.env, PYTHONPATH: SRC, MEDIA_VISUAL_BIND: '127.0.0.1',
         MEDIA_VISUAL_PORT: String(PORT), MEDIA_VISUAL_TRUST_TAILNET: '1',
         MEDIA_VISUAL_VIDEO: '0' },
  stdio: ['ignore', 'pipe', 'pipe'] });
srv.stderr.on('data', d => process.stdout.write('[srv!] ' + d));

function get(p) {
  return new Promise(res => {
    const r = http.get({ host: '127.0.0.1', port: PORT, path: p, timeout: 1500 },
      x => { let b=''; x.on('data',c=>b+=c); x.on('end',()=>res({s:x.statusCode,b})); });
    r.on('error', () => res(null)); r.on('timeout', () => { r.destroy(); res(null); });
  });
}

// Long enough that the panel genuinely scrolls at phone height. This matters:
// with a handful of short lines #tx has scrollHeight === clientHeight and the
// scroll route to the text ahead is not merely untested but IMPOSSIBLE, which
// is the whole reason a tap reveals it too. Both routes are checked below.
const LINES = Array.from({ length: 16 }, (_, i) =>
  `Sentence ${i + 1}: the pipeline reads each frame from the spool, hands it to `
  + `the renderer, and writes nothing back until the whole clip has landed.`);
const SHORT = [
  'One short line.',
  'And a second, which the voice has not reached.',
];

(async () => {
  let ok = true;
  const fail = (m) => { ok = false; console.log('FAIL  ' + m); };
  const pass = (m) => console.log('PASS  ' + m);
  for (let i = 0; i < 40; i++) { if (await get('/status')) break; await sleep(400); }

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 420, height: 780 },
                                       hasTouch: true, isMobile: true });
  await page.route('**/input', r => r.abort());
  await page.route('**/events*', r => r.abort());
  await page.addInitScript(() => {
    const Real = window.EventSource;
    window.EventSource = function (...a) { const es = new Real(...a); window.__es = es; return es; };
    window.EventSource.prototype = Real.prototype;
  });
  await page.goto(`http://127.0.0.1:${PORT}/`);
  await page.waitForTimeout(1200);

  const feed = (idx, speaking = true) => page.evaluate(({ lines, idx, speaking }) => {
    window.__es.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(
      { kind: 'state', speaking, sentence: lines[idx], lines, lidx: idx, session: 's1' }) }));
  }, { lines: LINES, idx, speaking });

  // ---- the transcript ----------------------------------------------------
  await feed(1);
  await page.waitForTimeout(500);

  const zoomShown = await page.evaluate(() =>
    getComputedStyle(document.getElementById('zoom')).display !== 'none');
  zoomShown ? pass('T1 the door appears once there are words')
            : fail('T1 #zoom hidden while a sentence is up');

  await page.click('#zoom');
  await page.waitForTimeout(400);

  const state = () => page.evaluate(() => {
    const ps = [...document.querySelectorAll('#txlines p')];
    return { n: ps.length,
             now: ps.findIndex(p => p.classList.contains('now')),
             ahead: ps.map(p => p.classList.contains('ahead')),
             open: !document.getElementById('tx').hidden,
             revealed: document.body.classList.contains('txahead'),
             live: !document.getElementById('txlive').hidden,
             scroll: document.getElementById('tx').scrollTop,
             sh: document.getElementById('tx').scrollHeight,
             ch: document.getElementById('tx').clientHeight,
             nowTop: (ps[ps.findIndex(p => p.classList.contains('now'))]||{}).offsetTop };
  });

  let s = await state();
  console.log(JSON.stringify(s));
  s.n === LINES.length ? pass('T2 the whole clip is there')
                       : fail(`T2 ${s.n} lines, expected ${LINES.length}`);
  s.now === 1 ? pass('T3 the live sentence is lit') : fail(`T3 now=${s.now}`);
  (!s.ahead[0] && s.ahead[2] && s.ahead[4])
    ? pass('T4 said is readable, ahead is masked')
    : fail(`T4 mask wrong: ${JSON.stringify(s.ahead)}`);
  await page.screenshot({ path: path.join(SHOTS, 'tx-open.png') });

  // The mask is not merely a class: it must actually be unreadable.
  const hidden = await page.evaluate(() => {
    const p = [...document.querySelectorAll('#txlines p')].find(x => x.classList.contains('ahead'));
    const cs = getComputedStyle(p);
    return { colour: cs.color, blur: cs.filter };
  });
  (hidden.colour.includes('rgba(0, 0, 0, 0)') || hidden.blur.includes('blur'))
    ? pass('T5 ahead of the voice is genuinely obscured')
    : fail(`T5 ahead is legible: ${JSON.stringify(hidden)}`);

  // ---- following, and reading on -----------------------------------------
  await feed(3);
  await page.waitForTimeout(600);
  s = await state();
  s.now === 3 ? pass('T6 the highlight follows the voice') : fail(`T6 now=${s.now}`);

  // The panel must actually be scrollable, or T7 would pass vacuously on a
  // route the reader does not have.
  s = await state();
  (s.sh > s.ch + 40) ? pass('T7a the clip is long enough to scroll')
                     : fail(`T7a not scrollable: ${s.sh} vs ${s.ch}`);

  // Scroll well past the live line: that is the deliberate act that uncovers.
  await page.evaluate(() => {
    const tx = document.getElementById('tx');
    tx.scrollTop = tx.scrollHeight;
    tx.dispatchEvent(new Event('scroll'));
  });
  await page.waitForTimeout(300);
  s = await state();
  s.revealed ? pass('T7 scrolling past the voice reveals what is ahead')
             : fail(`T7 still masked: ${JSON.stringify(s)}`);
  s.live ? pass('T8 and offers the way back') : fail('T8 no catch-up button');
  await page.screenshot({ path: path.join(SHOTS, 'tx-ahead.png') });

  await page.click('#txlive');
  await page.waitForTimeout(500);
  s = await state();
  (!s.revealed && !s.live) ? pass('T9 back to the voice re-covers it')
                           : fail(`T9 revealed=${s.revealed} live=${s.live}`);

  // ---- the short clip, where scrolling is not on offer ---------------------
  // A two-line clip fits the panel, so there is nowhere to scroll to. The tap
  // is the only way to the text ahead, and it has to work — otherwise short
  // replies hide their ending permanently.
  await page.evaluate((lines) => {
    window.__es.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(
      { kind: 'state', speaking: true, sentence: lines[0], lines, lidx: 0, session: 's1' }) }));
  }, SHORT);
  await page.waitForTimeout(500);
  s = await state();
  (s.n === 2 && s.sh <= s.ch + 4)
    ? pass('T9b a short clip has nowhere to scroll')
    : fail(`T9b unexpectedly scrollable: ${JSON.stringify(s)}`);
  await page.evaluate(() => {
    document.querySelector('#txlines p.ahead').click();
  });
  await page.waitForTimeout(250);
  s = await state();
  s.revealed ? pass('T9c tapping the masked line reveals it')
             : fail('T9c tap did not reveal');

  // ---- pinch --------------------------------------------------------------
  // Reading size is a stored preference, and the band has to re-reserve for it.
  const grew = await page.evaluate(() => {
    const before = getComputedStyle(document.getElementById('tx')).fontSize;
    document.documentElement.style.setProperty('--txscale', 1.8);
    localStorage.setItem('txscale', '1.8');
    const after = getComputedStyle(document.getElementById('tx')).fontSize;
    return { before: parseFloat(before), after: parseFloat(after) };
  });
  grew.after > grew.before * 1.5
    ? pass('T10 reading size scales the transcript')
    : fail(`T10 ${grew.before} -> ${grew.after}`);

  // ---- the divider ---------------------------------------------------------
  // Needs a figure on screen: the seam only exists where two halves do.
  const FIG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300">
<rect width="400" height="300" fill="#101820"/>
<text x="8" y="292" fill="#fff" font-size="16">BOTTOM LABEL</text></svg>`;
  await page.evaluate(() => document.getElementById('txclose').click());
  await page.waitForTimeout(200);
  await page.evaluate((u) => {
    window.__es.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(
      { kind: 'show', image: u, purpose: 'figure', session: 's1', t: 2 }) }));
  }, 'data:image/svg+xml;base64,' + Buffer.from(FIG).toString('base64'));
  await page.waitForTimeout(1400);
  await page.evaluate((lines) => {
    window.__es.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(
      { kind: 'state', speaking: true, sentence: lines[1], lines, lidx: 1, session: 's1' }) }));
  }, LINES);
  await page.waitForTimeout(600);

  const band = () => page.evaluate(() => ({
    on: document.body.classList.contains('subband'),
    handle: !document.getElementById('split').hidden,
    h: parseFloat(getComputedStyle(document.documentElement)
                    .getPropertyValue('--subband')),
  }));
  let b = await band();
  (b.on && b.handle) ? pass('T12 the seam has a handle when both halves exist')
                     : fail(`T12 ${JSON.stringify(b)}`);

  // Drag it up: the words take more of the screen.
  const before = b.h;
  await page.evaluate(() => {
    const el = document.getElementById('split');
    const r = el.getBoundingClientRect();
    const y = r.top + r.height / 2;
    const opts = (cy) => ({ pointerId: 1, clientX: 200, clientY: cy, bubbles: true });
    el.setPointerCapture = () => {}; el.releasePointerCapture = () => {};
    el.dispatchEvent(new PointerEvent('pointerdown', opts(y)));
    el.dispatchEvent(new PointerEvent('pointermove', opts(y - 160)));
    el.dispatchEvent(new PointerEvent('pointerup', opts(y - 160)));
  });
  await page.waitForTimeout(300);
  b = await band();
  (b.h > before + 100) ? pass('T13 dragging the seam gives the words more room')
                       : fail(`T13 ${before} -> ${b.h}`);
  await page.screenshot({ path: path.join(SHOTS, 'tx-split.png') });

  // And the transcript opens into that half, with the figure still up.
  await page.evaluate(() => document.getElementById('zoom').click());
  await page.waitForTimeout(400);
  const split = await page.evaluate(() => {
    const t = document.getElementById('tx').getBoundingClientRect();
    const img = [...document.querySelectorAll('.layer')].find(e => e.classList.contains('on'));
    return { txTop: t.top, vh: innerHeight, imgVisible: !!img && getComputedStyle(img).opacity === '1' };
  });
  (split.txTop > split.vh * 0.15 && split.imgVisible)
    ? pass('T14 the transcript opens into the words half, figure still up')
    : fail(`T14 ${JSON.stringify(split)}`);

  // ---- the working indicator ------------------------------------------------
  const work = await page.evaluate(() => {
    const before = document.getElementById('txwork').hidden;
    window.__txprobe(2);
    const after = document.getElementById('txwork').hidden;
    return { before, after, text: document.getElementById('txworkt').textContent };
  });
  (work.before && !work.after && /2 working/.test(work.text))
    ? pass('T15 the foot shows what is still being worked on')
    : fail(`T15 ${JSON.stringify(work)}`);

  await page.evaluate(() => document.getElementById('txclose').click());
  await page.waitForTimeout(300);
  const closed = await page.evaluate(() => document.getElementById('tx').hidden);
  closed ? pass('T11 it closes') : fail('T11 still open');

  // ---- the picture under the fingers ---------------------------------------
  // "Not very smooth" was mostly this: the zoom scaled about the centre of the
  // screen and ignored where the fingers were, so the picture slid out from
  // under the gesture and had to be chased. The test is geometric, not
  // perceptual — a point held under a focal spot must still be under it after
  // the zoom.
  const focal = await page.evaluate(() => {
    const p = window.__imgprobe;
    p.resetImg();
    const sx = 300, sy = 500;
    const at0 = p.at();
    const pointBefore = { x: (sx - at0.x) / at0.z, y: (sy - at0.y) / at0.z };
    p.zoomAbout(3, sx, sy);
    const at1 = p.at();
    // Where that same picture-point now lands on screen.
    return { z: at1.z,
             sx: pointBefore.x * at1.z + at1.x,
             sy: pointBefore.y * at1.z + at1.y };
  });
  (Math.abs(focal.sx - 300) < 1 && Math.abs(focal.sy - 500) < 1 && focal.z === 3)
    ? pass('T19 zoom holds the point under the fingers still')
    : fail(`T19 focal drifted to ${focal.sx.toFixed(1)},${focal.sy.toFixed(1)}`);

  const panned = await page.evaluate(() => {
    const p = window.__imgprobe;
    const before = p.at();
    p.panImg(-40, -25);
    const after = p.at();
    p.panImg(-99999, -99999);              // shove it well past the edge
    const clamped = p.at();
    return { before, after, clamped, w: innerWidth, h: innerHeight };
  });
  (panned.after.x === panned.before.x - 40 && panned.after.y === panned.before.y - 25)
    ? pass('T20 two-finger drag moves the picture')
    : fail(`T20 ${JSON.stringify(panned.after)}`);
  (panned.clamped.x >= -panned.w * (panned.clamped.z - 1) - 0.5 &&
   panned.clamped.x <= 0.5)
    ? pass('T21 and cannot be dragged off into the black')
    : fail(`T21 x=${panned.clamped.x} z=${panned.clamped.z} w=${panned.w}`);

  await page.evaluate(() => window.__imgprobe.resetImg());
  const reset = await page.evaluate(() => window.__imgprobe.at());
  (reset.z === 1 && reset.x === 0 && reset.y === 0)
    ? pass('T22 double-tap returns the picture whole')
    : fail(`T22 ${JSON.stringify(reset)}`);

  // ---- holding an old page --------------------------------------------------
  // The bug this is really here for: nothing in the client ever reloaded the
  // document, so a canvas restarted with new assets left every open screen on
  // the old page indefinitely — the wall, a phone tab, the app's WebView. The
  // stream reconnects; the stream is not the page.
  //
  // Watched as a real navigation rather than by stubbing location.reload,
  // which Chromium will not allow redefined. This also tests the thing that
  // actually matters: whether the document is fetched again.
  let navs = 0;
  page.on('framenavigated', (f) => { if (f === page.mainFrame()) navs++; });
  const hello = (id) => page.evaluate((p) => window.__es.dispatchEvent(
    new MessageEvent('message', { data: JSON.stringify({ kind: 'hello', page: p }) })), id);
  const settle = () => page.waitForTimeout(500);

  // The first hello is the page we are running. A second saying the same
  // thing must change nothing: canvases restart for reasons that have nothing
  // to do with the page, and blanking every screen in the house for those is
  // worse than the fault being fixed.
  await hello('same-as-loaded');
  await settle();
  await hello('same-as-loaded');
  await settle();
  navs === 0 ? pass('T16 an unchanged page does not reload the house')
             : fail(`T16 reloaded ${navs}x on an identical page id`);

  // Now one that disagrees — but with the voice still going, which must hold
  // it: a deploy should not blank the wall mid-sentence.
  await page.evaluate((lines) => window.__es.dispatchEvent(new MessageEvent('message',
    { data: JSON.stringify({ kind: 'state', speaking: true, sentence: lines[0],
                             lines, lidx: 0, session: 's1' }) })), LINES);
  await page.waitForTimeout(300);
  await hello('a-newer-page');
  await settle();
  navs === 0 ? pass('T17 a reload waits for the sentence to finish')
             : fail('T17 reloaded mid-reply');

  // ...and lands the moment the reply ends.
  await page.evaluate(() => window.__es.dispatchEvent(new MessageEvent('message',
    { data: JSON.stringify({ kind: 'state', speaking: false }) })));
  await page.waitForTimeout(1200);
  navs >= 1 ? pass('T18 and reloads onto the new page as soon as it does')
            : fail('T18 stayed on the old page');

  await browser.close();
  srv.kill();
  console.log(ok ? '\nALL PASS' : '\nFAILURES');
  process.exit(ok ? 0 : 1);
})().catch(async e => { console.error(e); srv.kill(); process.exit(1); });
