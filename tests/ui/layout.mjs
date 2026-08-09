/* Layout regression check: long filenames must stay inside their cards.
 *
 * Separate from e2e.mjs because it needs something jsdom does not have — a
 * layout engine. It drives a real Chromium and measures real boxes, through
 * the UI the way a person uses it: click a file in the pool, add a row, click
 * the slot.
 *
 * The bug this exists for: `grid-template-columns: 1fr 1fr` takes each track's
 * minimum from the item's min-content width, and a nowrap filename's
 * min-content is the whole string. One long name grew its column to 966px
 * inside a 708px card and pushed the slot 271px past the card's edge.
 *
 *   CHROME=/path/to/chromium BASE=http://127.0.0.1:8095 node layout.mjs
 *
 * See README.md for the fixture setup — it needs a source file with a long
 * name, which is the whole point of the test.
 */
import puppeteer from 'puppeteer-core';

const BASE = process.env.BASE || 'http://127.0.0.1:8090';
const CHROME = process.env.CHROME;
// Narrow enough to be a real window, wide enough to stay off the 1100px
// single-column breakpoint — the two-column case is where the bug lived.
const VIEWPORT = { width: 1280, height: 900 };
// Long enough that min-content exceeds the card: that is the failing case.
const MIN_NAME = 90;

if (!CHROME) {
  console.error('Set CHROME to a chromium/chrome binary. See README.md.');
  process.exit(2);
}

const checks = [];
const ck = (name, ok, extra = '') => {
  checks.push({ name, ok: !!ok });
  console.log(`${ok ? ' ok ' : 'FAIL'}  ${name}${!ok && extra ? '  → ' + extra : ''}`);
};
const rect = 'el => { const b = el.getBoundingClientRect();' +
             ' return { left: b.left, right: b.right, width: b.width }; }';

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: true,
  args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
         `--window-size=${VIEWPORT.width},${VIEWPORT.height}`],
  defaultViewport: VIEWPORT,
});

try {
  const page = await browser.newPage();
  page.on('pageerror', e => ck('no page errors', false, e.message));
  await page.goto(BASE, { waitUntil: 'networkidle2' });
  await page.waitForFunction(
    () => document.querySelectorAll('#filePool .chip').length > 0,
    { timeout: 30000 });

  const longName = await page.evaluate(() =>
    [...document.querySelectorAll('#filePool .chip .nm')]
      .map(n => n.textContent)
      .sort((a, b) => b.length - a.length)[0]);
  ck(`a filename of ${MIN_NAME}+ chars exists to test with`,
     longName.length >= MIN_NAME,
     `longest is ${longName.length} chars — see README.md`);
  if (longName.length < MIN_NAME) throw new Error('no long filename staged');

  await page.evaluate(name => {
    [...document.querySelectorAll('#filePool .chip')]
      .find(c => c.querySelector('.nm').textContent === name).click();
  }, longName);
  await page.click('#btnAddPair');
  await new Promise(r => setTimeout(r, 200));
  await page.click('#pairTable .card .drop');
  await new Promise(r => setTimeout(r, 300));

  const m = await page.evaluate(`(() => {
    const r = ${rect};
    const card = document.querySelector('#pairTable .card');
    const drop = document.querySelector('#pairTable .drop');
    const fn = document.querySelector('#pairTable .drop .fn');
    return {
      assigned: !!fn,
      card: r(card), drop: r(drop), fn: fn ? r(fn) : null,
      fnScroll: fn ? fn.scrollWidth : 0, fnClient: fn ? fn.clientWidth : 0,
      pageScroll: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
      cols: getComputedStyle(
        document.querySelector('#pairTable .pairgrid')).gridTemplateColumns,
    };
  })()`);

  ck('the file landed in the slot', m.assigned);
  ck('the name is too long for its box (the test is exercising the bug)',
     m.fnScroll > m.fnClient, `${m.fnScroll} needed vs ${m.fnClient} available`);
  ck('no horizontal page scroll',
     m.pageScroll <= m.viewport + 1, `page ${m.pageScroll} > viewport ${m.viewport}`);
  ck('the name stays inside the drop slot', m.fn.right <= m.drop.right + 1,
     `name ends at ${m.fn.right.toFixed(0)}, slot at ${m.drop.right.toFixed(0)}`);
  ck('the drop slot stays inside the card', m.drop.right <= m.card.right + 1,
     `slot ends at ${m.drop.right.toFixed(0)}, card at ${m.card.right.toFixed(0)}`);
  ck('the card stays inside the viewport', m.card.right <= m.viewport + 1,
     `card ends at ${m.card.right.toFixed(0)}, viewport ${m.viewport}`);
  // The two slots share the row: one name must not starve the other.
  const [a, b] = m.cols.split(' ').map(parseFloat);
  ck('both slots keep a fair share of the row',
     Math.min(a, b) / Math.max(a, b) > 0.8, `columns ${m.cols}`);
  console.log(`      card ${m.card.width.toFixed(0)}px · columns ${m.cols} · ` +
              `name box ${m.fnClient}px of ${m.fnScroll}px wanted`);

  // The edits tab puts a filename beside a duration and a pill in the same way.
  await page.evaluate(() => [...document.querySelectorAll('nav button')]
    .find(b => b.dataset.step === 'edits').click());
  await new Promise(r => setTimeout(r, 400));
  const e = await page.evaluate(
    '({ pageScroll: document.documentElement.scrollWidth,' +
    '   viewport: window.innerWidth })');
  ck('the edits tab does not scroll sideways either',
     e.pageScroll <= e.viewport + 1, `page ${e.pageScroll} > ${e.viewport}`);
} finally {
  await browser.close();
}

const bad = checks.filter(c => !c.ok);
console.log(`\n${checks.length - bad.length}/${checks.length} passed`);
if (bad.length) console.log('failed: ' + bad.map(c => c.name).join('; '));
process.exit(bad.length ? 1 : 0);
