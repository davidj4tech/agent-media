// Subtitle-band harness: over a FITTED image (a figure) the spoken sentence
// becomes a bottom band the picture makes room for, instead of a pill floating
// over the labels. 8 checks, ~15s. Asserts the picture ends above the words at
// one line and at three, that the band is flush to the bottom edge, that the
// reply dock rides above it AND clears the picture, and that ambient (cover)
// art still gets the old floating pill. Screenshots land in shots/.
//
// Unlike harness.js this one aborts /events: the throwaway instance reads the
// real house speech state, and a genuine speaking:false frame clears the
// injected sentence mid-measurement. The client still constructs an
// EventSource, which is all the synthetic frames need.
'use strict';
const { spawn } = require('child_process');
const http = require('http');
const path = require('path');
const fs = require('fs');
const { chromium } = require('playwright');

const REPO = '/home/ryer/projects/agent-media';
const SRC = path.join(REPO, 'packages', 'visual', 'src');
const PY = path.join(REPO, '.venv', 'bin', 'python');
const PORT = Number(process.env.MEDIA_BAND_PORT || 8793);
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

function post(p, body) {
  return new Promise(res => {
    const data = JSON.stringify(body);
    const req = http.request({ host: '127.0.0.1', port: PORT, path: p, method: 'POST',
      headers: { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data) } },
      r => { let b = ''; r.on('data', c => b += c); r.on('end', () => res({ s: r.statusCode, b })); });
    req.on('error', e => res({ err: String(e) }));
    req.end(data);
  });
}
function get(p) {
  return new Promise(res => {
    const r = http.get({ host: '127.0.0.1', port: PORT, path: p, timeout: 1500 },
      x => { let b=''; x.on('data',c=>b+=c); x.on('end',()=>res({s:x.statusCode,b})); });
    r.on('error', () => res(null)); r.on('timeout', () => { r.destroy(); res(null); });
  });
}

// A stand-in "figure": 4:3 SVG with labels hard against every edge.
const FIG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300">
<rect width="400" height="300" fill="#101820"/>
<text x="8" y="20" fill="#fff" font-size="16">TOP-LEFT LABEL</text>
<text x="8" y="292" fill="#fff" font-size="16">BOTTOM-LEFT LABEL</text>
<text x="392" y="292" fill="#fff" font-size="16" text-anchor="end">BOTTOM-RIGHT</text>
<rect x="120" y="110" width="160" height="80" fill="none" stroke="#7fd" stroke-width="3"/>
</svg>`;

(async () => {
  let ok = true;
  const fail = (m) => { ok = false; console.log('FAIL  ' + m); };
  const pass = (m) => console.log('PASS  ' + m);
  for (let i = 0; i < 40; i++) { if (await get('/status')) break; await sleep(400); }

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 900, height: 600 } });
  await page.route('**/input', r => r.abort());          // never inject into live tmux
  // Cut the live stream: the throwaway reads the real house speech state, and a
  // genuine speaking:false frame clears the injected sentence mid-measurement.
  // The client still constructs an EventSource, which is all we dispatch on.
  await page.route('**/events*', r => r.abort());
  await page.addInitScript(() => {
    const Real = window.EventSource;
    window.EventSource = function (...a) { const es = new Real(...a); window.__es = es; return es; };
    window.EventSource.prototype = Real.prototype;
  });
  await page.goto(`http://127.0.0.1:${PORT}/`);
  await page.waitForTimeout(1200);

  // Serve the figure from the page's own origin via a data: URL push.
  const dataUrl = 'data:image/svg+xml;base64,' + Buffer.from(FIG).toString('base64');
  await page.evaluate((u) => {
    window.__es.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(
      { kind: 'show', image: u, purpose: 'figure', session: 's1', t: 1 }) }));
  }, dataUrl);
  await page.waitForTimeout(1500);

  const feed = (sentence) => page.evaluate((s) => {
    window.__es.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(
      { kind: 'state', speaking: true, sentence: s, session: 's1', visual: true }) }));
  }, sentence);

  const geom = () => page.evaluate(() => {
    const img = [...document.querySelectorAll('.layer')].find(e => e.classList.contains('on'));
    const r = img.getBoundingClientRect();
    const s = document.getElementById('sub').getBoundingClientRect();
    // where the letterboxed picture actually paints inside the box
    const ar = img.naturalWidth / img.naturalHeight;
    const boxAr = r.width / r.height;
    const pw = boxAr > ar ? r.height * ar : r.width;
    const ph = boxAr > ar ? r.height : r.width / ar;
    const picBottom = r.top + (r.height + ph) / 2;
    return { band: document.body.classList.contains('subband'),
             subVar: getComputedStyle(document.documentElement).getPropertyValue('--subband'),
             boxBottom: r.bottom, picBottom, subTop: s.top, subBottom: s.bottom,
             inp: document.getElementById('inp').getBoundingClientRect().top,
             vh: window.innerHeight };
  });

  await feed('The pipeline reads from the spool, then hands each frame to the renderer.');
  await page.waitForTimeout(700);
  let g = await geom();
  console.log(JSON.stringify(g, null, 1));
  g.band ? pass('T1 band engages for a fitted figure') : fail('T1 no band class');
  if (g.picBottom <= g.subTop + 1) pass('T2 picture ends above the words');
  else fail(`T2 picture (${g.picBottom.toFixed(1)}) overlaps subtitle top (${g.subTop.toFixed(1)})`);
  if (Math.abs(g.subBottom - g.vh) < 2) pass('T3 band is flush to the bottom edge');
  else fail(`T3 band bottom ${g.subBottom} vs vh ${g.vh}`);
  if (g.inp <= g.subTop + 1) pass('T4 reply dock rides above the band');
  else fail(`T4 dock top ${g.inp} sits on the band (${g.subTop})`);
  if (g.picBottom <= g.inp + 1) pass('T4b picture clears the reply dock too');
  else fail(`T4b picture ${g.picBottom.toFixed(1)} under dock top ${g.inp}`);
  await page.screenshot({ path: path.join(SHOTS, 'band-1line.png') });

  // A long sentence wraps to more lines: the reserve must grow, not spill.
  await feed('A much longer sentence, the kind a reply actually produces when it '
    + 'is explaining something at length, which will wrap across several lines on '
    + 'this viewport and must push the figure up rather than spilling over it.');
  await page.waitForTimeout(700);
  const g2 = await geom();
  console.log(JSON.stringify(g2, null, 1));
  if (parseFloat(g2.subVar) > parseFloat(g.subVar)) pass('T5 reserve grows with the wrap');
  else fail(`T5 reserve did not grow: ${g.subVar} -> ${g2.subVar}`);
  if (g2.picBottom <= g2.subTop + 1) pass('T6 still no overlap when wrapped');
  else fail(`T6 overlap: pic ${g2.picBottom.toFixed(1)} > sub ${g2.subTop.toFixed(1)}`);
  await page.screenshot({ path: path.join(SHOTS, 'band-3line.png') });

  // Ambient art (cover) keeps the floating pill — nothing to protect there.
  await page.evaluate(() => {
    window.__es.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(
      { kind: 'show', image: 'data:image/svg+xml;base64,' +
        btoa('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300"><rect width="400" height="300" fill="#432"/></svg>'),
        session: 's1', t: 2 }) }));
  });
  await page.waitForTimeout(1600);
  const g3 = await geom();
  if (!g3.band && parseFloat(g3.subVar) === 0) pass('T7 ambient art keeps the floating pill');
  else fail(`T7 band stuck on for cover art: ${g3.band} ${g3.subVar}`);
  await page.screenshot({ path: path.join(SHOTS, 'band-ambient.png') });

  await browser.close();
  srv.kill();
  console.log(ok ? '\nALL PASS' : '\nFAILURES');
  process.exit(ok ? 0 : 2);
})().catch(e => { console.error(e); srv.kill(); process.exit(3); });
