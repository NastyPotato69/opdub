/* End-to-end UI test: the real frontend, in a real DOM, against the real
 * server, driving real ffmpeg. No stubbed responses anywhere.
 *
 * Walks the whole workflow the way a person would: pick folders, identify
 * tracks, choose an edit, mark the intro, queue the run, then open the
 * resulting EDL, nudge a cut and re-render.
 */
import { JSDOM, VirtualConsole } from 'jsdom';
import { readFileSync, existsSync } from 'fs';

const BASE = process.env.BASE || 'http://127.0.0.1:8090';
const HTML = '/workspace/opdub/static/index.html';
const JS = '/workspace/opdub/static/app.js';

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errors.push('jsdomError: ' + (e.stack || e.message)));
vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));

const dom = new JSDOM(readFileSync(HTML, 'utf8'), {
  runScripts: 'outside-only', pretendToBeVisual: true,
  virtualConsole: vc, url: 'http://localhost/',
});
const w = dom.window;

// Real network, absolute-ised onto the live server.
w.fetch = (url, opts) => fetch(new URL(String(url), BASE), opts);
w.EventSource = class { constructor() {} close() {} };   // polled instead
w.HTMLMediaElement.prototype.play = function () { return Promise.resolve(); };
w.HTMLMediaElement.prototype.pause = function () {};
w.localStorage.clear();
// jsdom has no <dialog> support in this version.
w.HTMLDialogElement && (w.HTMLDialogElement.prototype.showModal = function () { this.open = true; });
if (!w.HTMLElement.prototype.showModal) w.HTMLElement.prototype.showModal = function () {};
if (!w.HTMLElement.prototype.close) w.HTMLElement.prototype.close = function () {};

// jsdom ships no canvas. Provide a recording stub so the drawing code really
// runs (and would throw on a bad call) instead of being skipped by the guard.
const drawCalls = [];
w.HTMLCanvasElement.prototype.getContext = function () {
  const rec = name => (...a) => drawCalls.push([name, ...a]);
  return {
    scale: rec('scale'), setTransform: rec('setTransform'),
    clearRect: rec('clearRect'), fillRect: rec('fillRect'),
    strokeRect: rec('strokeRect'), fillText: rec('fillText'),
    measureText: () => ({ width: 10 }),
    fillStyle: '', strokeStyle: '', font: '', lineWidth: 1,
  };
};
// Layout is all zeroes in jsdom; give the canvases a believable width.
w.Element.prototype.getBoundingClientRect = function () {
  return { width: 900, height: 56, top: 0, left: 0, right: 900, bottom: 56, x: 0, y: 0 };
};

// `const S` does not leak between eval calls, so expose a handle for the test.
// Appended here rather than shipped in app.js.
w.eval(readFileSync(JS, 'utf8') +
  '\n;window.__t = { get S(){return S}, saveLocal, renderEdits, renderPairs,' +
  ' loadEdlList, openEdl, saveEdl };');

const wait = ms => new Promise(r => setTimeout(r, ms));
const $ = s => w.document.querySelector(s);
const $$ = s => [...w.document.querySelectorAll(s)];
const T = () => w.__t;
const state = expr => w.eval(`(function(S){ return (${expr}); })(window.__t.S)`);

const checks = [];
let step = '';
const sec = s => { step = s; console.log(`\n── ${s} ──`); };
const ck = (name, cond, extra = '') => {
  const ok = !!cond;
  checks.push({ step, name, ok });
  console.log(`${ok ? ' ok ' : 'FAIL'}  ${name}${!ok && extra ? '  → ' + extra : ''}`);
};

try {
  // ─────────────────────────── boot ───────────────────────────
  await wait(2500);
  sec('boot and folder defaults');
  ck('no runtime errors on boot', errors.length === 0, errors.join('\n'));
  ck('config reached the page', $('#rootsLabel').textContent.includes('/input'),
     $('#rootsLabel').textContent);

  const srcDir = state('S.sourceDir'), editDir = state('S.editDir');
  ck('sources folder defaulted to .../sources', srcDir.endsWith('/input/sources'), srcDir);
  ck('edits folder defaulted to .../edits', editDir.endsWith('/input/edits'), editDir);
  ck('sources path shown to the user', $('#srcDirLabel').textContent === srcDir);
  ck('edits path shown to the user', $('#editDirLabel').textContent === editDir);

  sec('folder dropdown is a browser, not a text box');
  ck('no free-text folder inputs remain',
     $('#srcDir') === null && $('#editDir') === null);
  const opts = $$('#srcDirPick option').map(o => o.value);
  ck('dropdown lists the input root', opts.some(o => o.endsWith('/input')), opts.join(','));
  ck('dropdown lists sources and edits',
     opts.some(o => o.endsWith('/sources')) && opts.some(o => o.endsWith('/edits')));
  ck('dropdown offers Browse…', opts.includes('__browse__'));
  ck('dropdown shows the current folder as selected', $('#srcDirPick').value === srcDir);
  const srcOpt = $$('#srcDirPick option').find(o => o.value === srcDir);
  ck('option shows a file count', /6 files/.test(srcOpt.textContent), srcOpt.textContent);

  sec('step 1 — sources loaded automatically');
  ck('files auto-loaded from the sources folder', state('S.sourceFiles.length') === 6,
     state('S.sourceFiles.length'));
  ck('file pool rendered', $$('#filePool .chip').length === 6,
     $$('#filePool .chip').length + ' chips');
  ck('chips show real stream tags',
     $('#filePool .chip').textContent.includes('jpn'), $('#filePool .chip').textContent);

  // ───────────────────── multitrack + auto-assign ─────────────────────
  sec('step 1 — multitrack mode and auto-assign');
  $$('#modeSwitch button').find(b => b.dataset.mode === 'multitrack').onclick();
  await wait(150);
  ck('multitrack pane visible', $('#modeMulti').style.display !== 'none');
  ck('one card per file', $$('#trackList > .card').length === 6,
     $$('#trackList > .card').length);
  ck('both tracks listed per file', $$('#trackList .stream').length === 12,
     $$('#trackList .stream').length);

  $('#btnAutoAssign').onclick();
  await wait(2500);
  ck('auto-assign paired every episode', state('S.pairs.length') === 6,
     state('S.pairs.length'));
  ck('jpn track identified as #1', state('S.pairs[0].jpn.stream') === 1);
  ck('eng track identified as #2', state('S.pairs[0].dub.stream') === 2);
  ck('report shows nothing left over',
     $('#assignReport').textContent.includes('nothing left over'),
     $('#assignReport').textContent);
  ck('ready count in the tab badge', $('#nSources').textContent === '6',
     $('#nSources').textContent);

  sec('step 1 — measure a real offset');
  const measureBtn = $$('#trackList button.btn').find(b => b.textContent === 'Measure');
  measureBtn.onclick();
  await wait(9000);
  const rep = state('JSON.stringify(S.pairs[0].offsetReport)');
  ck('offset measured and accepted', /"accepted":true/.test(rep), rep);
  ck('quality reported for review', /"quality":\d/.test(rep), rep);

  sec('step 1 — playback and track switching');
  const playEl = $('#trackList .stream button.play');
  playEl.onclick({ stopPropagation() {} });
  await wait(250);
  ck('now-playing bar appeared', $('#np').classList.contains('on'));
  ck('bar names the file and track', /edit|src/.test($('#npWhat').textContent),
     $('#npWhat').textContent);
  const sw = $$('#npTracks button');
  ck('switcher offers both tracks', sw.length === 2, sw.length + ' buttons');
  sw[1].onclick();
  await wait(250);
  ck('switching changes the reported track', $('#npWhat').textContent.includes('#2'),
     $('#npWhat').textContent);
  $('#npStop').onclick();
  ck('stop clears the bar', !$('#np').classList.contains('on'));

  // ───────────────────────────── step 2 ─────────────────────────────
  sec('step 2 — edits auto-loaded');
  ck('edit auto-loaded from the edits folder', state('S.edits.length') === 1,
     state('S.edits.length'));
  const editCard = $('#editList .card');
  ck('edit listed', editCard && editCard.textContent.includes('edit_00.mkv'));

  editCard.querySelector('input[type=checkbox]').checked = true;
  editCard.querySelector('input[type=checkbox]').onchange();
  await wait(150);
  ck('selecting the edit expands it', $('#editList .streams') !== null);

  const strm = $('#editList .stream');
  strm.onclick();
  await wait(150);
  ck('edit audio stream chosen', state('S.edits[0].stream') === 1,
     state('S.edits[0].stream'));

  const srcBoxes = $$('#editList label.check input[type=checkbox]');
  let ticked = 0;
  for (const b of srcBoxes) {
    if (b === editCard.querySelector('input[type=checkbox]')) continue;
    if (!b.checked) { b.checked = true; b.onchange(); ticked++; await wait(30); }
  }
  ck('source episodes ticked by hand', state('S.edits[0].sources.length') === 6,
     state('S.edits[0].sources.length'));

  sec('step 2 — mark the opening theme');
  state("S.edits[0].passthrough.push({t0:5,t1:15,label:'intro'})");
  T().saveLocal(); T().renderEdits();
  await wait(200);
  ck('passthrough range recorded', state('S.edits[0].passthrough.length') === 1);
  // The label lives in an <input value>, which textContent does not include.
  ck('range shown in the UI',
     $$('#editList input').some(i => i.value === 'intro'),
     $$('#editList input').map(i => i.value).join(','));
  // Warnings are expected here (five pairs have no measured offset) and are
  // deliberately non-blocking; only a red pill would stop the run.
  ck('no blocking problems on the edit', $('#editList .pill.bad') === null,
     $$('#editList .pill').map(p => p.textContent).join(','));

  // ───────────────────────────── step 3 ─────────────────────────────
  sec('step 3 — validate and run the real pipeline');
  $('#btnValidate').onclick();
  await wait(3000);
  ck('plan validated against real files',
     $('#validateOut').textContent.includes('ok'), $('#validateOut').textContent);

  $('#btnRun').onclick();
  await wait(1500);
  ck('job queued', state('S.jobs.length') >= 1, state('S.jobs.length'));

  let job = null;
  for (let i = 0; i < 120; i++) {
    const r = await (await fetch(BASE + '/api/jobs')).json();
    job = r.jobs[0];
    if (job && (job.state === 'done' || job.state === 'failed')) break;
    await wait(3000);
  }
  ck('pipeline finished', job && job.state === 'done',
     job ? `${job.state}: ${job.error || job.stage}` : 'no job');
  ck('EDL produced', job && job.outputs && job.outputs.edl);
  ck('WAV produced', job && job.outputs && job.outputs.wav);
  ck('MKV muxed', job && job.outputs && job.outputs.mkv);
  if (job?.outputs?.mkv) ck('MKV exists on disk', existsSync(job.outputs.mkv), job.outputs.mkv);

  // ───────────────────────────── step 4 ─────────────────────────────
  sec('step 4 — review, nudge, re-render');
  await T().loadEdlList();
  await wait(600);
  const edlOpts = $$('#edlPick option').map(o => o.value).filter(Boolean);
  ck('EDL listed for review', edlOpts.length >= 1, edlOpts.length);

  await T().openEdl(edlOpts[0]);
  await wait(600);
  ck('EDL opened', state('S.edl !== null'));
  const nseg = state('S.edl.segments.length');
  ck('segments present', nseg > 1, nseg);
  ck('passthrough carried into the EDL',
     state("S.edl.segments.filter(s=>s.status==='passthrough').length") === 1);
  ck('timeline legend rendered', $$('#tlLegend span').length >= 2,
     $$('#tlLegend span').length);
  ck('timeline actually drew segments',
     drawCalls.filter(c => c[0] === 'fillRect').length >= nseg,
     drawCalls.filter(c => c[0] === 'fillRect').length + ' fillRect calls');
  ck('segment table rendered', $$('#segList tbody tr').length >= 1,
     $$('#segList tbody tr').length);

  $$('#segList tbody tr')[0].onclick();
  await wait(200);
  ck('inspector opened for a segment', $('#inspector').textContent.includes('Segment'));

  const before = state('S.edl.segments[0].t1');
  const nudgeBtn = $$('#inspector button.btn').find(b => b.textContent === '+100 ms');
  ck('nudge controls offered', !!nudgeBtn);
  nudgeBtn.onclick();
  await wait(200);
  const after = state('S.edl.segments[0].t1');
  ck('cut moved by 100 ms', Math.abs((after - before) - 0.1) < 1e-6, `${before} → ${after}`);
  ck('next segment start followed the cut',
     Math.abs(state('S.edl.segments[1].t0') - after) < 1e-9);
  ck('EDL marked unsaved', state('S.edlDirty') === true);

  await T().saveEdl();
  await wait(800);
  ck('EDL saved', state('S.edlDirty') === false);
  ck('backup written beside it', existsSync(edlOpts[0] + '.bak'), edlOpts[0] + '.bak');

  $('#btnRerender').onclick();
  await wait(2000);
  let rr = null;
  for (let i = 0; i < 60; i++) {
    const r = await (await fetch(BASE + '/api/jobs')).json();
    rr = r.jobs.find(j => j.kind === 'rerender');
    if (rr && (rr.state === 'done' || rr.state === 'failed')) break;
    await wait(3000);
  }
  ck('re-render finished without re-aligning', rr && rr.state === 'done',
     rr ? `${rr.state}: ${rr.error || rr.stage}` : 'no rerender job');

  sec('overall');
  ck('no runtime errors during the whole run', errors.length === 0,
     errors.slice(0, 3).join('\n'));

} catch (e) {
  checks.push({ step, name: 'harness reached the end', ok: false });
  console.log('\nHARNESS THREW during: ' + step + '\n' + ((e && e.stack) || e));
}

const bad = checks.filter(c => !c.ok);
console.log(`\n${checks.length - bad.length}/${checks.length} passed`);
if (bad.length) console.log('failed: ' + bad.map(c => `[${c.step}] ${c.name}`).join('; '));
process.exit(bad.length ? 1 : 0);
