const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 420, height: 780 }, hasTouch: true });
  const frames = [];
  await p.addInitScript(() => {
    const R = window.EventSource;
    window.EventSource = function (...a) {
      const es = new R(...a);
      window.__es = es;
      window.__frames = [];
      es.addEventListener('message', (m) => { try { window.__frames.push(JSON.parse(m.data)); } catch (_) {} });
      return es;
    };
    window.EventSource.prototype = R.prototype;
  });
  await p.route('**/input', r => r.abort());
  await p.goto('http://red5:8781/', { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(6000);
  const out = await p.evaluate(() => ({
    bodyClasses: document.body.className,
    zoomDisplay: getComputedStyle(document.getElementById('zoom')).display,
    txLineCount: document.querySelectorAll('#txlines p').length,
    subOn: document.getElementById('sub').classList.contains('on'),
    splitHidden: document.getElementById('split').hidden,
    frames: (window.__frames || []).slice(-4).map(f => ({
      kind: f.kind, speaking: f.speaking,
      lines: Array.isArray(f.lines) ? f.lines.length : undefined,
      lidx: f.lidx, page: f.page })),
  }));
  console.log(JSON.stringify(out, null, 1));
  await p.screenshot({ path: 'shots/live-phone.png' });
  await b.close();
})();
