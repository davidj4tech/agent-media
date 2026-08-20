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
  // This used to assert the panel had NOWHERE to scroll, which was true when
  // the transcript covered the screen. It opens into the words' half now, so
  // even two lines can overflow a short strip — the premise died with the
  // split, not the guarantee. What matters is unchanged and tested below: the
  // tap reveals what is ahead, whether or not scrolling is on offer.
  (s.n === 2) ? pass('T9b a short clip renders both lines')
              : fail(`T9b ${JSON.stringify(s)}`);
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

  // A pinch that also drags must do both. This is the ordering bug that made
  // two-finger panning look dead: panning clamps against the CURRENT scale,
  // and at scale 1 there is no room, so a drag applied before the zoom was
  // thrown away before the zoom could make room for it.
  const both = await page.evaluate(() => {
    const p = window.__imgprobe;
    p.resetImg();                       // start at 1:1, where the bug lived
    p.zoomAbout(2.5, 210, 390);         // open it up about the fingers...
    p.panImg(-60, -45);                 // ...and drag in the same gesture
    return p.at();
  });
  (both.z === 2.5 && both.x < 0 && both.y < 0)
    ? pass('T23 zooming and dragging in one gesture keeps both')
    : fail(`T23 ${JSON.stringify(both)}`);

  await page.evaluate(() => window.__imgprobe.resetImg());
  const reset = await page.evaluate(() => window.__imgprobe.at());
  (reset.z === 1 && reset.x === 0 && reset.y === 0)
    ? pass('T22 double-tap returns the picture whole')
    : fail(`T22 ${JSON.stringify(reset)}`);

  // Double-tap a line to play from there, the way read-aloud works. A single
  // tap must NOT: the transcript is something you scroll and read, and a stray
  // touch that restarts the voice three paragraphs back would make it hostile.
  const seeks = [];
  await page.route('**/ctl', async (r) => {
    try {
      seeks.push({ body: JSON.parse(r.request().postData() || '{}'),
                   token: r.request().headers()['x-auth-token'] });
    } catch (_) {}
    await r.fulfill({ status: 200, contentType: 'application/json',
                      body: JSON.stringify({ ok: true, out: '' }) });
  });
  await page.evaluate(() => localStorage.setItem('amux_token', 'test-token'));
  await page.evaluate(() => document.getElementById('zoom').click());
  await page.waitForTimeout(300);
  // A tap on a still-masked line uncovers it and must NOT also seek —
  // uncovering is its own act.
  await page.evaluate(() => {
    document.body.classList.remove('txahead');
    const p = [...document.querySelectorAll('#txlines p')].find(
      (x) => x.classList.contains('ahead'));
    if (p) p.click();
  });
  await page.waitForTimeout(400);
  seeks.length === 0 ? pass('T29 tapping masked text uncovers, and does not seek')
                     : fail(`T29 issued ${JSON.stringify(seeks)}`);

  // One tap on a readable line plays from there. A scroll is a drag and a drag
  // does not fire click, which is why a single tap is safe here.
  await page.evaluate(() => {
    document.body.classList.add('txahead');
    document.querySelectorAll('#txlines p')[2].click();
  });
  await page.waitForTimeout(600);
  const seek = seeks[seeks.length - 1];
  (seek && seek.body.action === 'goto-sentence' && seek.body.sarg === '2')
    ? pass('T30 one tap plays from that sentence')
    : fail(`T30 ${JSON.stringify(seeks)}`);
  // /ctl is a state-changing POST and needs the token. act() used a bare
  // fetch, so every control action — every transport button, and this — came
  // back 401 and was swallowed. It looked like the controls doing nothing.
  (seek && seek.token === 'test-token')
    ? pass('T30b and carries the auth token')
    : fail(`T30b no token on the seek: ${JSON.stringify(seek)}`);

  const moved = await page.evaluate(() =>
    [...document.querySelectorAll('#txlines p')].findIndex(p => p.classList.contains('now')));
  moved === 2 ? pass('T31 and the highlight moves without waiting for the server')
              : fail(`T31 highlight at ${moved}`);
  await page.unroute('**/ctl');
  await page.evaluate(() => document.getElementById('txclose').click());
  await page.waitForTimeout(200);

  // The transcript must arrive on a frame that is NOT speaking — that is the
  // state a screen is in whenever anybody goes looking for what was just said,
  // and serving the lines only while speaking meant it was empty every time.
  const idle = await page.evaluate((lines) => {
    window.__es.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(
      { kind: 'state', speaking: false, lines, lidx: 3 }) }));
    return { n: document.querySelectorAll('#txlines p').length,
             door: getComputedStyle(document.getElementById('zoom')).display,
             masked: [...document.querySelectorAll('#txlines p')]
                       .filter((p) => p.classList.contains('ahead')).length };
  }, LINES);
  (idle.n === LINES.length && idle.door !== 'none')
    ? pass('T32 the transcript arrives with the voice stopped')
    : fail(`T32 ${JSON.stringify(idle)}`);
  idle.masked === 0 ? pass('T33 and nothing stays masked once nothing is coming')
                    : fail(`T33 ${idle.masked} lines still masked`);

  // The commonest case of all, and the one that stayed broken longest: words
  // with NO picture on the canvas. The band's gate demanded an image, so most
  // speech got a floating pill, no seam, no bottom half and no way into the
  // transcript — on a screen with room for all of it.
  const noPicture = await page.evaluate((lines) => {
    // Take the pictures away entirely.
    for (const el of document.querySelectorAll('.layer')) el.classList.remove('on');
    window.__es.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(
      { kind: 'state', speaking: true, sentence: lines[1], lines, lidx: 1, session: 's1' }) }));
    return new Promise((done) => setTimeout(() => done({
      band: document.body.classList.contains('subband'),
      seam: !document.getElementById('split').hidden,
      subVar: getComputedStyle(document.documentElement).getPropertyValue('--subband'),
    }), 400));
  }, LINES);
  (noPicture.band && noPicture.seam && parseFloat(noPicture.subVar) > 0)
    ? pass('T35 words with no picture still get the band and the seam')
    : fail(`T35 ${JSON.stringify(noPicture)}`);

  // ...and the transcript opens into that bottom half, not over everything.
  const half = await page.evaluate(() => {
    document.getElementById('sub').click();
    const t = document.getElementById('tx').getBoundingClientRect();
    return { top: t.top, vh: innerHeight, open: !document.getElementById('tx').hidden };
  });
  (half.open && half.top > 0)
    ? pass('T36 and the transcript opens into the bottom half')
    : fail(`T36 ${JSON.stringify(half)}`);
  await page.evaluate(() => document.getElementById('txclose').click());
  await page.waitForTimeout(200);

  // The band must never be a reserved strip of nothing. bandOn() is true
  // whenever there is a transcript, so an idle screen was carving a half out
  // of the picture and putting no words in it — which is what "still no"
  // looked like on the glass: a big empty space where a transcript should be.
  const idleBand = await page.evaluate((lines) => {
    window.__es.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(
      { kind: 'state', speaking: false, lines, lidx: 2 }) }));
    const sub = document.getElementById('sub');
    return { band: document.body.classList.contains('subband'),
             subOn: sub.classList.contains('on'),
             past: sub.classList.contains('past'),
             text: sub.textContent };
  }, LINES);
  (idleBand.band && idleBand.subOn && idleBand.past && idleBand.text === LINES[2])
    ? pass('T37 an idle band shows the last thing said, not nothing')
    : fail(`T37 ${JSON.stringify(idleBand)}`);

  // With captions off there is no band — and the transcript must still open as
  // a HALF, not cover the screen. It keyed off the band alone, so no band meant
  // inset:0, which reads as a pop-up and was reported as one.
  const noBandSplit = await page.evaluate((lines) => {
    localStorage.setItem('subs', '0');
    localStorage.removeItem('split');
    window.__es.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(
      { kind: 'state', speaking: false, lines, lidx: 0 }) }));
    document.getElementById('zoom').click();
    const t = document.getElementById('tx').getBoundingClientRect();
    const out = { band: document.body.classList.contains('subband'),
                  top: t.top, vh: innerHeight };
    document.getElementById('txclose').click();
    localStorage.setItem('subs', '1');
    return out;
  }, LINES);
  (!noBandSplit.band && noBandSplit.top > noBandSplit.vh * 0.35
                     && noBandSplit.top < noBandSplit.vh * 0.65)
    ? pass('T41 with no band the transcript still opens as a half')
    : fail(`T41 ${JSON.stringify(noBandSplit)}`);

  // The bottom half owns its taps. A tap that lands in the band but misses the
  // glyphs must open the transcript, NOT walk the mode ring the way a tap on
  // the picture does — "in the app a click on the bottom split is the same as
  // a click above".
  await page.evaluate(() => {
    const t = document.getElementById('tx');
    if (!t.hidden) document.getElementById('txclose').click();
  });
  await page.waitForTimeout(200);
  const strip = await page.evaluate(() => {
    const hit = document.getElementById('bandhit');
    const before = document.body.className;
    const shown = getComputedStyle(hit).display !== 'none';
    hit.click();                       // the strip, not the text
    return { shown, opened: !document.getElementById('tx').hidden,
             modeUnchanged: document.body.className.replace(' txopen', '') === before };
  });
  strip.shown ? pass('T38 the strip is a target, not just the glyphs')
              : fail('T38 no band hit area');
  strip.opened ? pass('T39 a tap beside the words still opens the transcript')
               : fail('T39 the tap fell through to the canvas');
  await page.evaluate(() => document.getElementById('txclose').click());
  await page.waitForTimeout(200);

  // Captions off means no band at all — never a reserved strip of nothing.
  const ccOff = await page.evaluate((lines) => {
    localStorage.setItem('subs', '0');
    window.__es.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(
      { kind: 'state', speaking: true, sentence: lines[0], lines, lidx: 0 }) }));
    const out = { band: document.body.classList.contains('subband'),
                  reserve: getComputedStyle(document.documentElement)
                             .getPropertyValue('--subband') };
    localStorage.setItem('subs', '1');
    return out;
  }, LINES);
  (!ccOff.band && parseFloat(ccOff.reserve) === 0)
    ? pass('T40 captions off reserves nothing')
    : fail(`T40 ${JSON.stringify(ccOff)}`);

  // The band itself opens the transcript. The corner icon was wearing the fit
  // toggle's own four-corner brackets, so the door onto the whole reply read
  // as "fullscreen" and went unpressed for days — the words are the obvious
  // target for asking to see more words.
  await page.evaluate(() => document.getElementById('txclose').click());
  await page.waitForTimeout(200);
  const viaBand = await page.evaluate(() => {
    document.getElementById('sub').click();
    return !document.getElementById('tx').hidden;
  });
  viaBand ? pass('T34 tapping the band opens the whole reply')
          : fail('T34 the band is not a door');
  await page.evaluate(() => document.getElementById('txclose').click());
  await page.waitForTimeout(200);

  // The door has to outlive the voice. It was tied to the subtitle being on
  // screen, so it vanished the moment the reply ended — leaving the transcript
  // reachable only while you were already being read to, and gone by the time
  // you thought "what did that say". Which is when you actually want it.
  const door = await page.evaluate((lines) => {
    const es = window.__es;
    es.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(
      { kind: 'state', speaking: true, sentence: lines[0], lines, lidx: 0, session: 's1' }) }));
    const whileSpeaking = getComputedStyle(document.getElementById('zoom')).display;
    es.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(
      { kind: 'state', speaking: false }) }));
    return { whileSpeaking, after: getComputedStyle(document.getElementById('zoom')).display };
  }, LINES);
  (door.whileSpeaking !== 'none' && door.after !== 'none')
    ? pass('T28 the transcript is still reachable after the voice stops')
    : fail(`T28 speaking=${door.whileSpeaking} after=${door.after}`);

  // Letting go of a pinch must not undo it. Two fingers lift a few
  // milliseconds apart, and every double-tap window is wider than that — so
  // the undo fired on the release of every single pinch and the zoom snapped
  // back to 1 the moment the fingers left the glass. Driven as real pointer
  // events, because the bug lives entirely in how they are counted.
  const survives = await page.evaluate(() => new Promise((done) => {
    const p = window.__imgprobe;
    p.resetImg();
    const ev = (type, id, x, y) => dispatchEvent(new PointerEvent(type,
      { pointerId: id, pointerType: 'touch', clientX: x, clientY: y, bubbles: true }));
    // A two-finger pinch: down, down, spread, then both up together.
    ev('pointerdown', 1, 180, 380);
    ev('pointerdown', 2, 240, 420);
    ev('pointermove', 1, 120, 320);
    ev('pointermove', 2, 300, 480);
    const zoomed = p.at().z;
    ev('pointerup', 1, 120, 320);
    ev('pointerup', 2, 300, 480);
    setTimeout(() => done({ zoomed, after: p.at().z }), 120);
  }));
  (survives.zoomed > 1.2 && Math.abs(survives.after - survives.zoomed) < 0.01)
    ? pass('T26 letting go of a pinch keeps the zoom')
    : fail(`T26 zoomed to ${survives.zoomed}, released to ${survives.after}`);

  // ...but a genuine double-tap still undoes it.
  const undone = await page.evaluate(() => new Promise((done) => {
    const p = window.__imgprobe;
    p.zoomAbout(3, 210, 390);
    const ev = (type, x, y) => dispatchEvent(new PointerEvent(type,
      { pointerId: 7, pointerType: 'touch', clientX: x, clientY: y, bubbles: true }));
    ev('pointerdown', 200, 400); ev('pointerup', 200, 400);
    setTimeout(() => {
      ev('pointerdown', 200, 400); ev('pointerup', 200, 400);
      setTimeout(() => done(p.at().z), 100);
    }, 80);
  }));
  undone === 1 ? pass('T27 and a real double-tap still returns it whole')
               : fail(`T27 still at ${undone}`);

  // The gesture handlers are useless if the browser is allowed to claim the
  // fingers first: with the default touch-action it takes any two-finger move
  // as its own scroll/zoom and pointercancels ours partway through, which is
  // exactly "one increment per pinch and panning does nothing". Cheap to
  // assert, and invisible to every other test here.
  const touch = await page.evaluate(() => ({
    body: getComputedStyle(document.body).touchAction,
    tx: getComputedStyle(document.getElementById('tx')).touchAction,
    split: getComputedStyle(document.getElementById('split')).touchAction,
  }));
  (touch.body === 'none' && touch.split === 'none')
    ? pass('T24 the page claims the gestures')
    : fail(`T24 ${JSON.stringify(touch)}`);
  (touch.tx === 'pan-y')
    ? pass('T25 except the transcript, which still scrolls')
    : fail(`T25 transcript touch-action is ${touch.tx}`);

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
