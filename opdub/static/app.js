/* opdub web UI.
 *
 * Guiding rule: this page never quietly decides anything. Wherever the
 * pipeline used to infer something — which file is Japanese, which stream is
 * the dub, which two files are the same episode, which sources an edit used,
 * where the opening theme is — the operator states it, and can listen to the
 * audio before committing.
 */
'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const S = {
  config: null,
  folders: [],    // every folder under the media roots, with media counts
  sourceMode: 'separate',   // 'separate' | 'multitrack'
  sourceDir: '',
  editDir: '',
  sourceFiles: [],          // probed files in the sources folder
  pairs: [],      // source episodes, hand-paired
  assignUnmatched: [],      // last auto-assign's reasons, keyed by path
  edits: [],      // edits with stream / sources / passthrough
  jobs: [],
  activeJob: null,
  edl: null, edlPath: null, edlDirty: false, segIdx: null,
  projectName: '',
};

/* Distinct hues per source index. Deliberately not a gradient — adjacent
 * sources must be told apart at a glance on the timeline. */
const SRC_COLORS = ['#58a6ff','#3fb950','#d29922','#bc8cff','#f778ba',
                    '#39c5cf','#ff7b72','#a5d6ff','#7ee787','#ffa657'];
const srcColor = i => SRC_COLORS[i % SRC_COLORS.length];
const PASSTHROUGH_COLOR = '#6e7681';

/* conf below this is worth a human look. Real segments run 200–47000. */
const LOW_CONF = 1000;

// ───────────────────────────── plumbing ─────────────────────────────

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json' }, ...opts,
  });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

function toast(msg, kind = 'info', ms = 4200) {
  const d = document.createElement('div');
  d.className = kind; d.textContent = msg;
  $('#toast').append(d);
  setTimeout(() => d.remove(), ms);
}

const pad = (n, w = 2) => String(n).padStart(w, '0');
function fmt(t) {
  if (t == null || Number.isNaN(t)) return '—';
  const neg = t < 0; t = Math.abs(t);
  const m = Math.floor(t / 60), s = Math.floor(t % 60);
  const ms = Math.round((t - Math.floor(t)) * 1000);
  return `${neg ? '-' : ''}${pad(m)}:${pad(s)}.${pad(ms, 3)}`;
}
function parseTime(str, fallback) {
  const s = String(str).trim();
  if (/^\d{1,3}:\d{1,2}(\.\d+)?$/.test(s)) {
    const [m, sec] = s.split(':');
    return parseInt(m, 10) * 60 + parseFloat(sec);
  }
  const v = parseFloat(s);
  return Number.isFinite(v) ? v : fallback;
}
const esc = s => String(s ?? '').replace(/[&<>"]/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const uid = () => Math.random().toString(36).slice(2, 10);

// ───────────────────────────── playback ─────────────────────────────

let currentAudio = null, currentBtn = null;

/* The preview endpoint hands back one decoded window at a time, so playing on
 * past it means chaining requests. 30 s is long enough that the chain is rare
 * and short enough that a scrub lands in well under a second. */
const CHUNK = 30;

/** What is playing right now: which window of which track, and how long the
 *  whole track is, so the scrub bar can address the entire file. */
const NP = {
  path: null, stream: null, name: '', streams: [],
  base: 0,      // file time this window starts at
  len: 0,       // length of the window that was requested
  fileDur: 0,   // whole-track duration; 0 while unknown
  chain: false, // keep loading the next window when this one ends
};

/** Durations learned from probes, so scrubbing a file that was never probed
 *  in this view (a dub referenced only by an EDL) still knows its length. */
const durCache = new Map();

function rememberDuration(pr) {
  if (pr?.path && pr.duration) durCache.set(pr.path, pr.duration);
}

async function ensureFileDur(path) {
  if (durCache.has(path)) return durCache.get(path);
  try {
    const pr = await probeOne(path);
    durCache.set(path, pr?.duration || 0);
  } catch (_) { durCache.set(path, 0); }
  return durCache.get(path);
}

/** Drop the audio element without touching what the bar says it is playing. */
function killAudio() {
  if (currentAudio) {
    currentAudio.onended = currentAudio.onerror = currentAudio.ontimeupdate = null;
    currentAudio.pause();
    currentAudio = null;
  }
}

function stopAudio() {
  killAudio();
  if (currentBtn) { currentBtn.classList.remove('playing'); currentBtn = null; }
  NP.path = null;
  renderNowPlaying();
}

/** Fetch and play one window. Everything else is bookkeeping around this. */
function playWindow(at, len) {
  killAudio();
  NP.base = Math.max(0, at);
  NP.len = len;

  const url = `/api/preview?path=${encodeURIComponent(NP.path)}&stream=${NP.stream}` +
              `&t=${NP.base.toFixed(3)}&dur=${len}`;
  const a = new Audio(url);
  currentAudio = a;

  a.onended = () => {
    // A window past the end of the file decodes to nothing and ends instantly;
    // that, not a duration check, is what reliably terminates the chain — the
    // track length is not always known.
    if (!NP.chain || !(a.duration > 0.25)) { stopAudio(); return; }
    if (NP.fileDur && NP.base + NP.len >= NP.fileDur - 0.05) { stopAudio(); return; }
    playWindow(NP.base + NP.len, CHUNK);
  };
  a.onerror = () => { toast('Could not play that stream', 'bad'); stopAudio(); };
  a.ontimeupdate = drawProgress;
  a.play().catch(() => {});

  renderNowPlaying();
  drawProgress();
}

/**
 * Start playback at `t`.
 *
 * `opts.snippet` keeps the old behaviour of stopping at the end of the
 * requested window — that is what makes the A/B compare buttons useful. Every
 * other button plays on until stopped, and any scrub switches to that mode.
 */
function play(path, stream, t, dur, btn, opts = {}) {
  const same = currentBtn === btn && currentAudio;
  stopAudio();
  if (same) return;                       // clicking the same button stops

  currentBtn = btn;
  if (btn) btn.classList.add('playing');

  NP.path = path; NP.stream = stream;
  NP.name = opts.name || path.split('/').pop();
  NP.streams = opts.streams || [];
  NP.chain = !opts.snippet;
  NP.fileDur = opts.duration || durCache.get(path) || 0;

  // Continuous playback starts on a full window so the first chain-on happens
  // half a minute in rather than after the six seconds a snippet asks for.
  playWindow(t, NP.chain ? Math.max(dur, CHUNK) : dur);

  if (!NP.fileDur) {
    // The bar is usable without it; it just cannot span the file until the
    // probe lands, so fill it in behind the playback rather than before it.
    ensureFileDur(path).then(d => {
      if (NP.path === path) { NP.fileDur = d; drawProgress(); }
    });
  }
}

/** Switch to another track of the same file, continuing where we are.
 *  Hearing the same instant in both languages is the whole point. */
function switchTrack(stream) {
  if (!NP.path || !currentAudio) return;
  const elapsed = currentAudio.currentTime || 0;
  const at = NP.base + elapsed;
  NP.stream = stream;
  // A snippet keeps its remaining length, so an A/B compare stays an A/B
  // compare after the switch instead of running on into the next scene.
  playWindow(at, NP.chain ? CHUNK : Math.max(2, NP.len - elapsed));
}

/** Jump anywhere in the track. Scrubbing means the operator is hunting for
 *  speech, so it always plays on from there rather than stopping at a window. */
function seekTo(t) {
  if (!NP.path) return;
  NP.chain = true;
  playWindow(Math.max(0, t), CHUNK);
}

/** Length the scrub bar spans: the whole track once known, the decoded window
 *  until then, so the head never runs off the end of the bar. */
const npSpan = () => NP.fileDur || (NP.base + NP.len) || 1;

let scrubAt = null;      // ghost position while the pointer is down

function drawProgress() {
  if (!NP.path) return;
  const span = npSpan();
  const at = scrubAt != null ? scrubAt : NP.base + (currentAudio?.currentTime || 0);
  const pct = Math.max(0, Math.min(100, at / span * 100));

  $('#npAt').textContent = fmt(at);
  $('#npDur').textContent = NP.fileDur ? fmt(NP.fileDur) : '—';
  $('#npFill').style.width = `${pct}%`;
  $('#npHead').style.left = `${pct}%`;
  // The loaded window, so a head that jumps when a window ends makes sense.
  const w0 = Math.max(0, Math.min(100, NP.base / span * 100));
  const w1 = Math.max(0, Math.min(100, (NP.base + NP.len) / span * 100));
  $('#npWin').style.left = `${w0}%`;
  $('#npWin').style.width = `${Math.max(0.5, w1 - w0)}%`;
}

function renderNowPlaying() {
  const bar = $('#np');
  if (!NP.path) { bar.classList.remove('on'); return; }
  bar.classList.add('on');

  const cur = NP.streams.find(s => s.index === NP.stream);
  const tag = cur ? (cur.language || cur.title || `stream ${cur.index}`) : `stream ${NP.stream}`;
  $('#npWhat').textContent = `${NP.name} — track #${NP.stream} (${tag}) from ${fmt(NP.base)}`;

  const host = $('#npTracks');
  host.innerHTML = '';
  if (NP.streams.length > 1) {
    host.append(Object.assign(document.createElement('span'),
      { className: 'hint', textContent: 'switch:' }));
    for (const s of NP.streams) {
      const b = document.createElement('button');
      if (s.index === NP.stream) b.className = 'cur';
      const lbl = s.language || (s.title ? s.title.slice(0, 14) : `${s.codec || 'audio'}`);
      b.textContent = `#${s.index} ${lbl}`;
      b.title = `${s.codec || ''} ${s.channels || ''}ch ${s.title || ''}`.trim();
      b.onclick = () => switchTrack(s.index);
      host.append(b);
    }
  }
}

$('#npStop').onclick = stopAudio;

// ── scrubbing ──
{
  const bar = $('#npBar');
  const timeAt = ev => {
    const r = bar.getBoundingClientRect();
    const f = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
    return f * npSpan();
  };
  bar.onmousedown = ev => {
    if (!NP.path) return;
    ev.preventDefault();
    scrubAt = timeAt(ev);
    bar.classList.add('scrubbing');
    drawProgress();
  };
  window.addEventListener('mousemove', ev => {
    if (scrubAt == null) return;
    scrubAt = timeAt(ev);
    drawProgress();
  });
  window.addEventListener('mouseup', () => {
    if (scrubAt == null) return;
    const t = scrubAt;
    scrubAt = null;
    bar.classList.remove('scrubbing');
    seekTo(t);
  });
}

/** A listen button. Defaults to mid-file, which is where speech lives —
 *  the opening theme is identical in every language and proves nothing. */
function playBtn(path, stream, t, dur = 6, label = '▶', opts = {}) {
  const b = document.createElement('button');
  b.className = 'play'; b.textContent = label; b.title = `Listen at ${fmt(t)}`;
  b.onclick = e => { e.stopPropagation(); play(path, stream, t, dur, b, opts); };
  return b;
}

/** Mid-file, clamped so short files still land inside the audio. */
const midpoint = pr => Math.max(5, (pr?.duration || 600) / 2);

/** Everything a listen button needs to describe what it is playing. */
const listenOpts = pr => ({
  name: pr?.name, streams: pr?.audio || [], duration: pr?.duration || 0 });

// ─────────────────────────── persistence ────────────────────────────

/** Probe blobs are dropped on the way out and re-attached on load — they are
 *  re-derivable from the server and would otherwise blow the storage quota. */
const stripProbe = p => p && ({ ...p, probe: null });

function saveLocal() {
  try {
    localStorage.setItem('opdub', JSON.stringify({
      sourceMode: S.sourceMode,
      sourceDir: S.sourceDir,
      pairs: S.pairs.map(p => ({ ...p, jpn: stripProbe(p.jpn), dub: stripProbe(p.dub) })),
      edits: S.edits.map(e => ({ ...e, probe: null, wave: null,
                                 waveView: null, waveSel: null })),
      editDir: S.editDir, projectName: S.projectName,
    }));
  } catch (_) {}
}

async function restoreLocal() {
  let d;
  try { d = JSON.parse(localStorage.getItem('opdub') || 'null'); } catch (_) { return; }
  if (!d) return;
  S.pairs = d.pairs || [];
  S.projectName = d.projectName || '';
  S.sourceMode = d.sourceMode === 'multitrack' ? 'multitrack' : 'separate';
  S.sourceDir = d.sourceDir || '';
  if (d.editDir) S.editDir = d.editDir;
  for (const e of (d.edits || [])) {
    try { e.probe = (await api('/api/probe', {
      method: 'POST', body: JSON.stringify({ paths: [e.path] }) })).files[0];
    } catch (_) { e.probe = null; }
    S.edits.push(e);
  }
}

// ────────────────────────── file picker ─────────────────────────────

let pickResolve = null;

let pickMode = 'file';   // 'file' | 'dir'
let pickHere = '';       // folder currently shown, for "Use this folder"

async function pickPath(title, startDir, mode = 'file') {
  pickMode = mode;
  $('#pickTitle').textContent = title;
  $('#pickFoot').style.display = mode === 'dir' ? '' : 'none';
  await browseInto(startDir || S.config.roots[0]?.path || '');
  $('#pickDlg').showModal();
  return new Promise(res => { pickResolve = res; });
}

const pickFile = (title, startDir) => pickPath(title, startDir, 'file');
const pickFolder = (title, startDir) => pickPath(title, startDir, 'dir');

function closePick(value) {
  $('#pickDlg').close();
  const r = pickResolve; pickResolve = null;
  if (r) r(value ?? null);
}

$('#pickUse').onclick = () => closePick(pickHere);
$('#pickUp').onclick = () => {
  const up = pickHere.replace(/\/[^/]+\/?$/, '') || '/';
  browseInto(up).catch(() => toast('Cannot go above the media roots', 'bad'));
};

async function browseInto(dir) {
  const list = $('#pickList');
  list.innerHTML = '<p class="empty">Loading…</p>';
  try {
    const d = await api(`/api/browse?dir=${encodeURIComponent(dir)}`);
    pickHere = d.dir;
    $('#pickDir').textContent = d.dir;
    list.innerHTML = '';

    const roots = document.createElement('div');
    roots.className = 'row'; roots.style.marginBottom = '10px';
    for (const r of S.config.roots) {
      const b = document.createElement('button');
      b.className = 'btn sm'; b.textContent = r.path;
      b.onclick = () => browseInto(r.path);
      roots.append(b);
    }
    // Shortcuts straight to nested folders that hold media, so a deep input
    // tree does not have to be clicked through one level at a time.
    for (const m of S.folders.filter(f => f.files).slice(0, 12)) {
      if (m.rel === '.') continue;
      const b = document.createElement('button');
      b.className = 'btn sm';
      b.textContent = `${m.rel} (${m.files})`;
      b.title = m.path;
      b.onclick = () => browseInto(m.path);
      roots.append(b);
    }
    list.append(roots);

    const t = document.createElement('table');
    t.innerHTML = '<thead><tr><th>Name</th><th style="width:110px">Size</th></tr></thead>';
    const tb = document.createElement('tbody');
    for (const sub of d.dirs) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>📁 ${esc(sub.name)}</td><td></td>`;
      tr.style.cursor = 'pointer';
      tr.onclick = () => browseInto(sub.path);
      tb.append(tr);
    }
    for (const f of d.files) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>🎬 ${esc(f.name)}</td>` +
        `<td class="hint mono">${(f.size / 1048576).toFixed(0)} MB</td>`;
      tr.style.cursor = 'pointer';
      if (pickMode === 'dir') { tr.style.cursor = 'default'; tr.style.opacity = '.55'; }
      else tr.onclick = () => closePick(f.path);
      tb.append(tr);
    }
    t.append(tb);
    if (!d.dirs.length && !d.files.length) {
      list.append(Object.assign(document.createElement('p'),
        { className: 'empty', textContent: 'Nothing here.' }));
    } else list.append(t);
  } catch (e) {
    list.innerHTML = `<p class="empty hint bad">${esc(e.message)}</p>`;
  }
}

$('#pickClose').onclick = () => closePick(null);
$('#pickDlg').addEventListener('close', () => { pickResolve?.(null); pickResolve = null; });

async function probeOne(path) {
  const r = await api('/api/probe', {
    method: 'POST', body: JSON.stringify({ paths: [path] }) });
  return r.files[0];
}

/** Stream chooser. Language tags are shown as evidence, never preselected —
 *  a wrong tag choosing the stream is the exact bug this replaces. */
function streamChooser(pr, picked, onPick) {
  const box = document.createElement('div');
  box.className = 'streams';
  if (!pr || pr.error) {
    box.innerHTML = `<p class="hint bad">${esc(pr?.error || 'probe failed')}</p>`;
    return box;
  }
  const mid = (pr.duration || 600) / 2;
  for (const a of pr.audio) {
    const row = document.createElement('div');
    row.className = 'stream' + (picked === a.index ? ' picked' : '');
    row.onclick = () => onPick(a.index);
    const tag = a.language ? `lang=${a.language}` : 'lang=untagged';
    const title = a.title ? ` · ${esc(a.title)}` : '';
    row.innerHTML =
      `<span class="idx">#${a.index}</span>` +
      `<span>${esc(a.codec || '?')} · ${a.channels || '?'}ch</span>` +
      `<span class="tag">${esc(tag)}${title}</span>`;
    const sp = document.createElement('span'); sp.style.flex = '1'; row.append(sp);
    row.append(playBtn(pr.path, a.index, mid, 6, '▶ listen', listenOpts(pr)));
    box.append(row);
  }
  if (!pr.audio.length) box.innerHTML = '<p class="hint bad">No audio streams.</p>';
  return box;
}

// ───────────────────────── step 1: sources ──────────────────────────
//
// Two library layouts, one underlying model. Whichever mode is active, a
// "pair" is always {jpn: {path, stream}, dub: {path, stream}} — in separate
// mode the two paths differ, in multi-track mode they are the same file with
// two stream indices. Steps 2-4 never need to know which mode was used.

const MODE_HINTS = {
  separate: 'Each file holds one language, so files must be paired with each ' +
            'other. Drag from the list into a row, or click a file then click a slot.',
  multitrack: 'Each file holds several audio tracks, so nothing needs pairing — ' +
              'only the tracks need identifying. Play them to check.',
};

let poolPick = null;    // file clicked in the pool, waiting for a slot
let dragPath = null;    // survives browsers with awkward dataTransfer rules

function setSourceMode(mode) {
  if (S.sourceMode === mode) return;
  S.sourceMode = mode;
  poolPick = null;
  saveLocal();
  renderPairs();
}

$$('#modeSwitch button').forEach(b => {
  b.onclick = () => setSourceMode(b.dataset.mode);
});

function assignedPaths() {
  const s = new Set();
  for (const p of S.pairs) {
    if (p.jpn?.path) s.add(p.jpn.path);
    if (p.dub?.path) s.add(p.dub.path);
  }
  return s;
}

function fileByPath(path) {
  return S.sourceFiles.find(f => f.path === path) || null;
}

function pairForFile(path) {
  return S.pairs.find(p => p.jpn?.path === path || p.dub?.path === path) || null;
}

function newPair(label, { top = false } = {}) {
  const p = { id: uid(), label: label || `Episode ${S.pairs.length + 1}`,
              jpn: null, dub: null, offset: null, offsetReport: null };
  if (top) S.pairs.unshift(p); else S.pairs.push(p);
  return p;
}

/** Attach a file to one side of a pair. Stream is only auto-filled when the
 *  file has exactly one audio track — that is arithmetic, not a guess. */
function assignToSlot(pair, role, path) {
  const pr = fileByPath(path);
  if (!pr) return;
  const audio = pr.audio || [];
  pair[role] = {
    path, name: pr.name, probe: pr,
    stream: audio.length === 1 ? audio[0].index : null,
  };
  pair.offset = null; pair.offsetReport = null;
  poolPick = null;
  saveLocal(); renderPairs();
}

function clearSlot(pair, role) {
  pair[role] = null;
  pair.offset = null; pair.offsetReport = null;
  saveLocal(); renderPairs();
}

// ── main render ──

function renderPairs() {
  $('#nSources').textContent = S.pairs.filter(
    p => p.jpn?.stream != null && p.dub?.stream != null).length;
  $('#modeHint').textContent = MODE_HINTS[S.sourceMode] || '';
  $$('#modeSwitch button').forEach(
    b => b.classList.toggle('active', b.dataset.mode === S.sourceMode));
  $('#modeSeparate').style.display = S.sourceMode === 'separate' ? '' : 'none';
  $('#modeMulti').style.display = S.sourceMode === 'multitrack' ? '' : 'none';

  if (S.sourceMode === 'separate') { renderPool(); renderPairTable(); }
  else { renderMultitrack(); }
  renderAssignStatus();
}

// ── mode A: the file pool ──

function streamBadges(pr) {
  const wrap = document.createElement('span');
  wrap.className = 'row'; wrap.style.gap = '4px';
  for (const a of (pr.audio || [])) {
    const s = document.createElement('span');
    const tag = a.language || (a.title ? a.title.slice(0, 10) : 'untagged');
    s.className = 'pill' + (a.language ? ' info' : '');
    s.textContent = `#${a.index} ${tag}`;
    s.title = `${a.codec || ''} ${a.channels || ''}ch ${a.title || ''}`.trim();
    wrap.append(s);
  }
  if (!(pr.audio || []).length) {
    wrap.append(Object.assign(document.createElement('span'),
      { className: 'pill bad', textContent: 'no audio' }));
  }
  return wrap;
}

function renderPool() {
  const host = $('#filePool');
  host.innerHTML = '';
  const used = assignedPaths();
  const hide = $('#hideAssigned').checked;
  const shown = S.sourceFiles.filter(f => !(hide && used.has(f.path)));

  if (!S.sourceFiles.length) {
    host.innerHTML = '<p class="empty">Load a sources folder to see its files.</p>';
    return;
  }
  if (!shown.length) {
    host.innerHTML = '<p class="empty">Every file is assigned.</p>';
    return;
  }

  for (const pr of shown) {
    const chip = document.createElement('div');
    chip.className = 'chip' + (used.has(pr.path) ? ' used' : '') +
                     (poolPick === pr.path ? ' picked' : '');
    chip.draggable = true;

    chip.ondragstart = ev => {
      dragPath = pr.path;
      ev.dataTransfer.setData('text/plain', pr.path);
      ev.dataTransfer.effectAllowed = 'copy';
      chip.classList.add('dragging');
    };
    chip.ondragend = () => { chip.classList.remove('dragging'); dragPath = null; };
    chip.onclick = () => {
      poolPick = poolPick === pr.path ? null : pr.path;
      renderPool();
    };

    const nm = document.createElement('span');
    nm.className = 'nm'; nm.textContent = pr.name; nm.title = pr.name;
    chip.append(nm, streamBadges(pr));

    const first = (pr.audio || [])[0];
    if (first) {
      chip.append(playBtn(pr.path, first.index, midpoint(pr), 6, '▶', listenOpts(pr)));
    }
    host.append(chip);
  }
}

// ── mode A: the episode table ──

function dropSlot(pair, role) {
  const slot = pair[role];
  const box = document.createElement('div');
  box.className = 'drop ' + (role === 'dub' ? 'eng' : 'jpn');

  box.ondragover = ev => { ev.preventDefault(); box.classList.add('over'); };
  box.ondragleave = () => box.classList.remove('over');
  box.ondrop = ev => {
    ev.preventDefault();
    box.classList.remove('over');
    const path = ev.dataTransfer.getData('text/plain') || dragPath;
    if (path) assignToSlot(pair, role, path);
  };

  if (!slot) {
    box.classList.add('empty');
    box.textContent = poolPick
      ? `click to put ${fileByPath(poolPick)?.name.slice(0, 28) || 'file'} here`
      : (role === 'dub' ? 'drop the English file' : 'drop the Japanese file');
    box.onclick = () => { if (poolPick) assignToSlot(pair, role, poolPick); };
    return box;
  }

  const top = document.createElement('div');
  top.className = 'row'; top.style.gap = '6px';
  const fn = document.createElement('span');
  fn.className = 'fn'; fn.style.flex = '1'; fn.textContent = slot.name; fn.title = slot.name;
  top.append(fn);

  const audio = slot.probe?.audio || [];
  if (slot.stream != null) {
    top.append(playBtn(slot.path, slot.stream, midpoint(slot.probe), 6, '▶',
                       { ...listenOpts(slot.probe), name: slot.name }));
  }
  const x = document.createElement('button');
  x.className = 'play'; x.textContent = '✕'; x.title = 'remove';
  x.onclick = ev => { ev.stopPropagation(); clearSlot(pair, role); };
  top.append(x);
  box.append(top);

  if (audio.length > 1) {
    const sel = document.createElement('select');
    sel.style.fontSize = '11.5px';
    sel.innerHTML = '<option value="">— pick the track —</option>' +
      audio.map(a => `<option value="${a.index}" ${a.index === slot.stream ? 'selected' : ''}>` +
        `#${a.index} ${esc(a.language || a.title || a.codec || 'audio')}</option>`).join('');
    sel.onchange = () => {
      slot.stream = sel.value === '' ? null : parseInt(sel.value, 10);
      pair.offset = null; pair.offsetReport = null;
      saveLocal(); renderPairs();
    };
    box.append(sel);
  } else if (slot.stream == null) {
    box.append(Object.assign(document.createElement('span'),
      { className: 'hint bad', textContent: 'this file has no audio track' }));
  }
  return box;
}

function renderPairTable() {
  const host = $('#pairTable');
  host.innerHTML = '';

  if (!S.pairs.length) {
    host.innerHTML = '<p class="empty">No episodes yet. Press ' +
      '<b>Assign automatically</b>, or add a row and drag files in.</p>';
    return;
  }

  for (const p of S.pairs) {
    const card = document.createElement('div');
    card.className = 'card tight'; card.style.marginBottom = '10px';

    const head = document.createElement('div');
    head.className = 'row';
    // Episode labels are short ("Episode 3", "One Piece 384"). Letting the
    // field grow to the full card width strands the name on the left and the
    // buttons on the right with a metre of nothing between them.
    const name = document.createElement('input');
    name.value = p.label; name.style.width = 'min(280px,100%)';
    name.oninput = () => { p.label = name.value; saveLocal(); refreshEditCards(); };
    head.append(name);

    if (p.reason) {
      const why = document.createElement('span');
      why.className = 'pill'; why.textContent = 'auto'; why.title = p.reason;
      head.append(why);
    }
    head.append(Object.assign(document.createElement('span'),
      { style: 'flex:1' }));
    const del = document.createElement('button');
    del.className = 'btn sm danger'; del.textContent = 'Remove';
    del.onclick = () => {
      S.pairs = S.pairs.filter(x => x.id !== p.id);
      for (const e of S.edits) e.sources = e.sources.filter(id => id !== p.id);
      saveLocal(); renderPairs(); refreshEditCards();
    };
    head.append(del);
    card.append(head);

    const hdr = document.createElement('div');
    hdr.className = 'pairhead'; hdr.style.marginTop = '9px';
    hdr.innerHTML = '<span>English — the dub</span>' +
                    '<span>Japanese — used for alignment</span>';
    card.append(hdr);

    const grid = document.createElement('div');
    grid.className = 'pairgrid';
    grid.append(dropSlot(p, 'dub'), dropSlot(p, 'jpn'));
    card.append(grid);

    card.append(offsetUI(p));
    host.append(card);
  }
}

/* New rows go on top: the row you just made is the one you are about to drag
 * files into, and a long library would otherwise push it off the screen. */
$('#btnAddPair').onclick = () => {
  newPair(`Episode ${S.pairs.length + 1}`, { top: true });
  saveLocal(); renderPairs();
};

$('#hideAssigned').onchange = renderPool;

// ── mode B: identify tracks inside one file ──

function setRole(pr, streamIndex, role) {
  let pair = pairForFile(pr.path);
  if (!pair) {
    pair = newPair(pr.name.replace(/\.[^.]+$/, ''));
  }
  const other = role === 'jpn' ? 'dub' : 'jpn';
  // The same track cannot be both languages.
  if (pair[other]?.path === pr.path && pair[other]?.stream === streamIndex) {
    pair[other] = null;
  }
  pair[role] = { path: pr.path, name: pr.name, probe: pr, stream: streamIndex };
  pair.offset = null; pair.offsetReport = null;
  saveLocal(); renderPairs();
}

function clearRole(pr, role) {
  const pair = pairForFile(pr.path);
  if (!pair) return;
  pair[role] = null;
  pair.offset = null; pair.offsetReport = null;
  if (!pair.jpn && !pair.dub) {
    S.pairs = S.pairs.filter(x => x.id !== pair.id);
    for (const e of S.edits) e.sources = e.sources.filter(id => id !== pair.id);
  }
  saveLocal(); renderPairs();
}

function renderMultitrack() {
  const host = $('#trackList');
  host.innerHTML = '';

  if (!S.sourceFiles.length) {
    host.innerHTML = '<div class="card"><p class="empty">' +
      'Load a sources folder to see its files.</p></div>';
    return;
  }

  for (const pr of S.sourceFiles) {
    const pair = pairForFile(pr.path);
    const audio = pr.audio || [];

    const card = document.createElement('div');
    card.className = 'card'; card.style.marginBottom = '10px';

    const head = document.createElement('div');
    head.className = 'row';
    head.append(Object.assign(document.createElement('b'), { textContent: pr.name }));
    const sp = document.createElement('span'); sp.style.flex = '1'; head.append(sp);
    head.append(Object.assign(document.createElement('span'), {
      className: 'hint mono',
      textContent: pr.duration ? fmt(pr.duration) : '' }));

    const done = pair?.jpn?.stream != null && pair?.dub?.stream != null;
    head.append(Object.assign(document.createElement('span'), {
      className: 'pill ' + (done ? 'ok' : audio.length < 2 ? 'bad' : 'warn'),
      textContent: done ? 'both tracks set'
        : audio.length < 2 ? `${audio.length} audio track`
        : 'needs identifying' }));
    card.append(head);

    if (pr.error) {
      card.append(Object.assign(document.createElement('p'),
        { className: 'hint bad', textContent: pr.error }));
      host.append(card);
      continue;
    }

    const list = document.createElement('div');
    list.className = 'streams'; list.style.marginTop = '10px';

    for (const a of audio) {
      const isJpn = pair?.jpn?.path === pr.path && pair.jpn.stream === a.index;
      const isEng = pair?.dub?.path === pr.path && pair.dub.stream === a.index;

      const row = document.createElement('div');
      row.className = 'stream' + (isJpn || isEng ? ' picked' : '');

      row.innerHTML =
        `<span class="idx">#${a.index}</span>` +
        `<span>${esc(a.codec || '?')} · ${a.channels || '?'}ch</span>` +
        `<span class="tag">${esc(a.language ? 'lang=' + a.language : 'untagged')}` +
        `${a.title ? ' · ' + esc(a.title) : ''}</span>`;

      const spacer = document.createElement('span');
      spacer.style.flex = '1'; row.append(spacer);

      if (isJpn) row.append(Object.assign(document.createElement('span'),
        { className: 'pill info', textContent: 'Japanese' }));
      if (isEng) row.append(Object.assign(document.createElement('span'),
        { className: 'pill warn', textContent: 'English' }));

      const bj = document.createElement('button');
      bj.className = 'btn sm'; bj.textContent = isJpn ? 'unset jpn' : 'Japanese';
      bj.onclick = () => isJpn ? clearRole(pr, 'jpn') : setRole(pr, a.index, 'jpn');
      const be = document.createElement('button');
      be.className = 'btn sm'; be.textContent = isEng ? 'unset eng' : 'English';
      be.onclick = () => isEng ? clearRole(pr, 'dub') : setRole(pr, a.index, 'dub');
      row.append(bj, be);

      row.append(playBtn(pr.path, a.index, midpoint(pr), 8, '▶ listen', listenOpts(pr)));
      list.append(row);
    }
    card.append(list);

    if (audio.length > 1) {
      card.append(Object.assign(document.createElement('p'), {
        className: 'hint', style: 'margin-top:6px',
        textContent: 'Press ▶ on one track, then use the switcher in the bar ' +
                     'at the bottom to jump between tracks at the same moment.' }));
    }
    if (pair) card.append(offsetUI(pair));
    host.append(card);
  }
}

// ── shared: the jpn→dub offset ──

function offsetUI(pair) {
  const box = document.createElement('div');
  box.style.marginTop = '10px';
  box.style.paddingTop = '10px';
  box.style.borderTop = '1px solid var(--border)';

  const sameFile = pair.jpn?.path && pair.jpn.path === pair.dub?.path;
  const ready = pair.jpn?.stream != null && pair.dub?.stream != null;

  const row = document.createElement('div');
  row.className = 'row';
  row.append(Object.assign(document.createElement('span'),
    { className: 'hint', textContent: 'jpn → dub offset' }));

  const inp = document.createElement('input');
  inp.type = 'number'; inp.step = '0.001'; inp.className = 'mono';
  inp.value = pair.offset != null ? pair.offset.toFixed(4) : '';
  inp.placeholder = sameFile ? '0 (same file)' : 'measure or type';
  inp.onchange = () => {
    const v = parseFloat(inp.value);
    pair.offset = Number.isFinite(v) ? v : null;
    pair.offsetReport = pair.offset != null
      ? { accepted: true, reason: 'set by hand', quality: null } : null;
    saveLocal(); renderPairs();
  };
  row.append(inp, Object.assign(document.createElement('span'),
    { className: 'hint', textContent: 's' }));

  const meas = document.createElement('button');
  meas.className = 'btn sm'; meas.textContent = 'Measure';
  meas.disabled = !ready;
  meas.onclick = async () => {
    meas.disabled = true; meas.textContent = 'Measuring…';
    try {
      const r = await api('/api/offset', {
        method: 'POST',
        body: JSON.stringify({
          jpn_path: pair.jpn.path, jpn_stream: pair.jpn.stream,
          dub_path: pair.dub.path, dub_stream: pair.dub.stream }),
      });
      pair.offsetReport = r;
      pair.offset = r.accepted ? r.offset : null;
      if (!r.accepted) toast(`${pair.label}: ${r.reason}`, 'bad', 9000);
      saveLocal(); renderPairs();
    } catch (e) {
      toast(`Measure failed: ${e.message}`, 'bad');
      meas.disabled = false; meas.textContent = 'Measure';
    }
  };
  row.append(meas);
  if (!ready) {
    row.append(Object.assign(document.createElement('span'),
      { className: 'hint', textContent: 'set both tracks first' }));
  }
  box.append(row);

  const rep = pair.offsetReport;
  if (rep) {
    const p = document.createElement('p');
    p.style.marginTop = '6px';
    if (rep.accepted) {
      p.className = 'hint';
      p.innerHTML = `<span class="pill ok">measured</span> ` +
        (rep.quality != null
          ? `peak/rms ${rep.quality.toFixed(1)} — a matched pair scores well above 2.`
          : 'set by hand.');
    } else {
      p.className = 'hint bad';
      p.innerHTML = `<span class="pill bad">rejected</span> ${esc(rep.reason)}` +
        ` Raw reading was ${(rep.offset * 1000).toFixed(0)} ms. ` +
        `Leave blank to use 0 ms, or type a value.`;
    }
    box.append(p);
  }

  if (ready) {
    const cmp = document.createElement('div');
    cmp.className = 'row'; cmp.style.marginTop = '8px';
    const t = midpoint(pair.jpn.probe);
    cmp.append(Object.assign(document.createElement('span'),
      { className: 'hint', textContent: 'Same scene?' }));
    cmp.append(playBtn(pair.jpn.path, pair.jpn.stream, t, 6, '▶ jpn',
                       { ...listenOpts(pair.jpn.probe), name: pair.jpn.name, snippet: true }));
    cmp.append(playBtn(pair.dub.path, pair.dub.stream, t - (pair.offset || 0), 6, '▶ dub',
                       { ...listenOpts(pair.dub.probe), name: pair.dub.name, snippet: true }));
    cmp.append(Object.assign(document.createElement('span'), {
      className: 'hint',
      textContent: 'Both should be the same moment in different languages.' }));
    box.append(cmp);
  }
  return box;
}

// ── loading the folder, and auto-assign ──

$('#srcDirPick').onchange = async () => {
  const v = $('#srcDirPick').value;
  if (v === '__browse__') {
    $('#srcDirPick').value = S.sourceDir || '';
    const p = await pickFolder('Choose the sources folder', S.sourceDir);
    if (p) { setFolder('sources', p); await $('#btnLoadSources').onclick(); }
    return;
  }
  if (!v) return;
  setFolder('sources', v);
  await $('#btnLoadSources').onclick();
};

$('#srcBrowse').onclick = async () => {
  const p = await pickFolder('Choose the sources folder', S.sourceDir);
  if (p) { setFolder('sources', p); await $('#btnLoadSources').onclick(); }
};

$('#btnLoadSources').onclick = async () => {
  const dir = (S.sourceDir || '').trim();
  if (!dir) { toast('Pick a sources folder first', 'bad'); return; }
  const btn = $('#btnLoadSources');
  btn.disabled = true; btn.textContent = 'Loading…';
  try {
    const d = await api(`/api/browse?dir=${encodeURIComponent(dir)}`);
    const probes = await api('/api/probe', {
      method: 'POST', body: JSON.stringify({ paths: d.files.map(f => f.path) }) });
    S.sourceFiles = probes.files;
    probes.files.forEach(rememberDuration);
    setFolder('sources', d.dir);

    // Re-attach probe data to any pairs restored from a saved setup.
    for (const p of S.pairs) {
      for (const role of ['jpn', 'dub']) {
        if (p[role]?.path) p[role].probe = fileByPath(p[role].path) || p[role].probe;
      }
    }
    saveLocal(); renderPairs();
    if (!probes.files.length) {
      toast(`No media in ${d.dir} — put your source episodes there, ` +
            `or pick another folder`, 'info', 7000);
    } else {
      toast(`${probes.files.length} file(s) in ${d.dir}`, 'ok');
    }
  } catch (e) {
    toast(e.message, 'bad');
  } finally {
    btn.disabled = false; btn.textContent = 'Reload';
  }
};

$('#btnClearPairs').onclick = () => {
  if (!S.pairs.length) return;
  S.pairs = [];
  S.assignUnmatched = [];
  for (const e of S.edits) e.sources = [];
  saveLocal(); renderPairs(); refreshEditCards();
};

$('#btnAutoAssign').onclick = async () => {
  if (!S.sourceFiles.length) { toast('Load a sources folder first', 'bad'); return; }
  const btn = $('#btnAutoAssign');
  btn.disabled = true; btn.textContent = 'Working…';
  try {
    const r = await api('/api/auto-assign', {
      method: 'POST',
      body: JSON.stringify({ mode: S.sourceMode,
                             paths: S.sourceFiles.map(f => f.path) }),
    });

    S.pairs = r.pairs.map(p => ({
      id: uid(),
      label: p.label,
      reason: p.reason,
      jpn: p.jpn ? { path: p.jpn.path, name: p.jpn.path.split('/').pop(),
                     stream: p.jpn.stream, probe: fileByPath(p.jpn.path) } : null,
      dub: p.dub ? { path: p.dub.path, name: p.dub.path.split('/').pop(),
                     stream: p.dub.stream, probe: fileByPath(p.dub.path) } : null,
      offset: null, offsetReport: null,
    }));
    for (const e of S.edits) e.sources = [];

    S.assignUnmatched = r.unmatched || [];
    saveLocal(); renderPairs(); refreshEditCards();
    toast(`Assigned ${r.pairs.length}, left ${r.unmatched.length} for you`,
          r.unmatched.length ? 'info' : 'ok');
  } catch (e) {
    toast(e.message, 'bad', 9000);
  } finally {
    btn.disabled = false; btn.textContent = '✨ Assign automatically';
  }
};

/** The status line above the pools.
 *
 * Counted from the current state on every render, never from what auto-assign
 * happened to return — dragging a file in has to move these numbers, and a
 * stale "3 unassigned" next to a full table is worse than no status at all.
 * Auto-assign only contributes its per-file reasons, and only for files that
 * are still sitting unassigned.
 */
function renderAssignStatus() {
  const host = $('#assignReport');
  host.innerHTML = '';
  if (!S.sourceFiles.length && !S.pairs.length) return;

  const ready = S.pairs.filter(
    p => p.jpn?.stream != null && p.dub?.stream != null).length;
  const partial = S.pairs.length - ready;
  const used = assignedPaths();
  const left = S.sourceFiles.filter(f => !used.has(f.path));

  const sum = document.createElement('div');
  sum.className = 'row';
  sum.innerHTML =
    `<span class="pill ${ready ? 'ok' : ''}">${ready} episode${
      ready === 1 ? '' : 's'} ready</span>` +
    (partial ? `<span class="pill warn">${partial} incomplete</span>` : '') +
    (left.length
      ? `<span class="pill warn">${left.length} file${
          left.length === 1 ? '' : 's'} unassigned</span>`
      : `<span class="pill ok">nothing left over</span>`);
  host.append(sum);

  if (!left.length) return;

  const why = new Map((S.assignUnmatched || []).map(u => [u.path, u.why]));
  const det = document.createElement('details');
  det.style.marginTop = '7px';
  det.innerHTML = '<summary class="hint" style="cursor:pointer">' +
    'Which files are still unassigned</summary>';
  const ul = document.createElement('div');
  ul.style.cssText = 'margin-top:6px;display:flex;flex-direction:column;gap:3px';
  for (const f of left) {
    ul.insertAdjacentHTML('beforeend',
      `<div class="hint"><span class="mono">${esc(f.name)}</span>` +
      (why.has(f.path) ? ` — ${esc(why.get(f.path))}` : '') + `</div>`);
  }
  det.append(ul);
  host.append(det);
}

// ────────────────────────── step 2: edits ───────────────────────────

/** Populate both folder dropdowns. Every folder under the roots is listed,
 *  indented to read like a tree, so a path never has to be typed. */
async function loadFolders() {
  try {
    S.folders = (await api('/api/folders')).folders || [];
  } catch (_) { S.folders = []; }

  const multiRoot = S.config.roots.length > 1;
  const opts = S.folders.map(f => {
    const pad = '  '.repeat(f.depth);
    const base = f.rel === '.'
      ? (multiRoot ? f.root : f.root.split('/').pop() || f.root)
      : f.name;
    const count = f.files ? ` · ${f.files} file${f.files === 1 ? '' : 's'}` : ' · empty';
    return `<option value="${esc(f.path)}" title="${esc(f.path)}">` +
           `${pad}${esc(base)}${count}</option>`;
  }).join('') + '<option value="__browse__">Browse…</option>';

  for (const [sel, key] of [['#editDirPick', 'editDir'], ['#srcDirPick', 'sourceDir']]) {
    const el = $(sel);
    el.innerHTML = opts;
    if (S[key]) el.value = S[key];
  }
}

/** Conventional layout first: <root>/sources and <root>/edits. Falls back to
 *  a name that merely contains the word, then to whichever folder holds the
 *  most media, then to the root itself. */
function defaultFolder(kind) {
  if (!S.folders.length) return '';
  const want = kind === 'sources' ? 'sources' : 'edits';
  const alt = kind === 'sources' ? /sourc|orig|raw/i : /edit|pace|fan/i;

  const exact = S.folders.find(f => f.name.toLowerCase() === want);
  if (exact) return exact.path;

  const near = S.folders.filter(f => f.rel !== '.' && alt.test(f.name));
  if (near.length) return near.sort((a, b) => b.files - a.files)[0].path;

  const withMedia = S.folders.filter(f => f.files);
  if (withMedia.length) return withMedia.sort((a, b) => b.files - a.files)[0].path;

  return S.folders[0].path;
}

function setFolder(kind, path) {
  if (!path) return;
  if (kind === 'sources') {
    S.sourceDir = path;
    $('#srcDirPick').value = S.folders.some(f => f.path === path) ? path : '';
    $('#srcDirLabel').textContent = path;
  } else {
    S.editDir = path;
    $('#editDirPick').value = S.folders.some(f => f.path === path) ? path : '';
    $('#editDirLabel').textContent = path;
  }
  saveLocal();
}

$('#editDirPick').onchange = async () => {
  const v = $('#editDirPick').value;
  if (v === '__browse__') {
    $('#editDirPick').value = S.editDir || '';
    const p = await pickFolder('Choose the edits folder', S.editDir);
    if (p) { setFolder('edits', p); await $('#btnLoadEdits').onclick(); }
    return;
  }
  if (!v) return;
  setFolder('edits', v);
  await $('#btnLoadEdits').onclick();
};

$('#editBrowse').onclick = async () => {
  const p = await pickFolder('Choose the edits folder', S.editDir);
  if (p) { setFolder('edits', p); await $('#btnLoadEdits').onclick(); }
};

$('#btnLoadEdits').onclick = async () => {
  const dir = (S.editDir || '').trim();
  if (!dir) { toast('Pick an edits folder first', 'bad'); return; }
  const btn = $('#btnLoadEdits');
  btn.disabled = true; btn.textContent = 'Loading…';
  try {
    const d = await api(`/api/browse?dir=${encodeURIComponent(dir)}`);
    const known = new Set(S.edits.map(e => e.path));
    const probes = await api('/api/probe', {
      method: 'POST',
      body: JSON.stringify({ paths: d.files.map(f => f.path) }) });
    for (const pr of probes.files) {
      rememberDuration(pr);
      if (known.has(pr.path)) continue;
      // Selected by default: a folder of edits is normally a whole arc you
      // want to run, and unticking the odd one is less work than ticking all.
      S.edits.push({ path: pr.path, name: pr.name, probe: pr, stream: null,
                     sources: [], passthrough: [], selected: true, wave: null });
    }
    setFolder('edits', d.dir);
    saveLocal(); renderEdits();
    if (!probes.files.length) {
      toast(`No media in ${d.dir} — put your edits there, or pick another ` +
            `folder`, 'info', 7000);
    } else {
      toast(`${probes.files.length} file(s) in ${d.dir}`, 'ok');
    }
  } catch (e) {
    toast(e.message, 'bad');
  } finally {
    btn.disabled = false; btn.textContent = 'Reload';
  }
};

function refreshEditCards() { renderEdits(); }

/* Every waveform hangs mouseup/resize handlers off window, and renderEdits
 * throws the whole list away on any change. Without this the handlers pile up
 * on canvases that are no longer in the document. */
let waveHandlers = new AbortController();

function renderEdits() {
  const host = $('#editList');
  waveHandlers.abort();
  waveHandlers = new AbortController();
  host.innerHTML = '';
  $('#nEdits').textContent = S.edits.filter(e => e.selected).length;

  if (!S.edits.length) {
    host.innerHTML = '<p class="empty">Load a folder of edits to begin.</p>';
    return;
  }

  for (const e of S.edits) {
    const card = document.createElement('div');
    card.className = 'card';

    const head = document.createElement('div');
    head.className = 'row';
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = e.selected;
    cb.onchange = () => { e.selected = cb.checked; saveLocal(); renderEdits(); };
    const lbl = document.createElement('label');
    lbl.className = 'check'; lbl.append(cb);
    lbl.append(Object.assign(document.createElement('b'), { textContent: e.name }));
    head.append(lbl);
    const sp = document.createElement('span'); sp.style.flex = '1'; head.append(sp);
    head.append(Object.assign(document.createElement('span'), {
      className: 'hint mono',
      textContent: e.probe?.duration ? fmt(e.probe.duration) : '' }));
    head.append(statusPill(e));
    card.append(head);

    if (e.selected) {
      const body = document.createElement('div');
      body.style.marginTop = '12px';

      const h1 = document.createElement('h3');
      h1.textContent = 'Japanese audio stream of this edit';
      body.append(h1);
      body.append(streamChooser(e.probe, e.stream, idx => {
        e.stream = idx; e.wave = null; saveLocal(); renderEdits();
      }));
      if (e.stream == null) {
        body.append(Object.assign(document.createElement('p'), {
          className: 'hint warn', style: 'margin:6px 0 0',
          textContent: 'Listen and pick. Many edits have one untagged stream — ' +
                       'confirm it is the Japanese track anyway.' }));
      }

      const h2 = document.createElement('h3');
      h2.style.marginTop = '14px';
      h2.textContent = 'Source episodes this edit was cut from';
      body.append(h2);
      body.append(sourcePicker(e));

      const h3 = document.createElement('h3');
      h3.style.marginTop = '14px';
      h3.textContent = 'Opening theme and other passthrough ranges';
      body.append(h3);
      body.append(passthroughUI(e));

      card.append(body);
    }
    host.append(card);
  }
}

function statusPill(e) {
  const problems = editProblems(e);
  const warnings = editWarnings(e);
  const s = document.createElement('span');
  if (!e.selected) { s.className = 'pill'; s.textContent = 'not queued'; return s; }
  if (problems.length) {
    s.className = 'pill bad'; s.textContent = `${problems.length} to fix`;
    s.title = problems.join('\n');
  } else if (warnings.length) {
    s.className = 'pill warn'; s.textContent = `${warnings.length} warning`;
    s.title = warnings.join('\n');
  } else { s.className = 'pill ok'; s.textContent = 'ready'; }
  return s;
}

/** Blocking: the run cannot proceed without these. */
function editProblems(e) {
  const out = [];
  if (e.stream == null) out.push('No audio stream chosen for the edit.');
  if (!e.sources.length) out.push('No source episodes ticked.');
  for (const id of e.sources) {
    const p = S.pairs.find(x => x.id === id);
    if (!p) { out.push('A ticked source no longer exists.'); continue; }
    if (p.jpn?.stream == null) out.push(`${p.label}: Japanese stream not chosen.`);
    // A dub file with no stream picked would silently render as silence,
    // which is indistinguishable from a successful run until you listen.
    if (p.dub && p.dub.stream == null) out.push(`${p.label}: dub stream not chosen.`);
  }
  return out;
}

/** Non-blocking, but you should know before you spend an hour on the run. */
function editWarnings(e) {
  const out = [];
  if (!e.passthrough.length) {
    out.push('No opening theme marked — the theme is identical across the ' +
             'arc, so alignment there is unreliable.');
  }
  for (const id of e.sources) {
    const p = S.pairs.find(x => x.id === id);
    if (!p) continue;
    if (!p.dub) out.push(`${p.label}: no dub file assigned — segments from ` +
                         `this episode will be silent.`);
    else if (p.offset == null) {
      out.push(`${p.label}: dub offset not measured — 0 ms will be used.`);
    }
  }
  return out;
}

function sourcePicker(e) {
  const box = document.createElement('div');
  if (!S.pairs.length) {
    box.innerHTML = '<p class="hint bad">No source episodes in the library yet — ' +
                    'add them in step 1.</p>';
    return box;
  }
  const grid = document.createElement('div');
  grid.className = 'row'; grid.style.marginTop = '6px';
  for (const p of S.pairs) {
    const l = document.createElement('label');
    l.className = 'check';
    l.style.cssText = 'border:1px solid var(--border);border-radius:6px;padding:5px 10px';
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = e.sources.includes(p.id);
    cb.onchange = () => {
      e.sources = cb.checked ? [...e.sources, p.id] : e.sources.filter(x => x !== p.id);
      saveLocal(); renderEdits();
    };
    const dot = document.createElement('i');
    const idx = e.sources.indexOf(p.id);
    dot.style.cssText = `width:10px;height:10px;border-radius:3px;display:block;` +
      `background:${idx >= 0 ? srcColor(idx) : 'var(--border)'}`;
    l.append(cb, dot, document.createTextNode(p.label));
    grid.append(l);
  }
  box.append(grid);
  box.append(Object.assign(document.createElement('p'), {
    className: 'hint', style: 'margin-top:6px',
    textContent: 'Only ticked episodes are fingerprinted. Fewer means faster, ' +
                 'and a missing one shows up as silence rather than a wrong match.' }));
  return box;
}

// ─────────────────── passthrough (opening theme) ────────────────────

function passthroughUI(e) {
  const box = document.createElement('div');

  box.append(Object.assign(document.createElement('p'), {
    className: 'hint', style: 'margin:4px 0 8px',
    innerHTML: 'The opening theme is identical across every episode of the arc, ' +
      'so alignment there is a coin flip. Mark it and the edit keeps its own ' +
      'audio for that range — nothing is matched, nothing is replaced.' }));

  const waveWrap = document.createElement('div');
  if (e.stream == null) {
    waveWrap.innerHTML = '<p class="hint warn">Pick the edit\'s audio stream first.</p>';
  } else if (!e.wave) {
    const b = document.createElement('button');
    b.className = 'btn sm'; b.textContent = 'Load waveform';
    b.onclick = async () => {
      b.disabled = true; b.textContent = 'Decoding…';
      try {
        // 4000 is the server's ceiling. It costs one decode either way, and
        // the extra resolution is what zooming has to draw from.
        e.wave = await api(`/api/waveform?path=${encodeURIComponent(e.path)}` +
                           `&stream=${e.stream}&points=4000`);
        renderEdits();
      } catch (err) { toast(err.message, 'bad'); b.disabled = false;
                      b.textContent = 'Load waveform'; }
    };
    waveWrap.append(b);
    waveWrap.append(Object.assign(document.createElement('span'), {
      className: 'hint', style: 'margin-left:8px',
      textContent: 'Takes a few seconds; cached afterwards.' }));
  } else {
    waveWrap.append(waveCanvas(e));
  }
  box.append(waveWrap);

  const list = document.createElement('div');
  list.style.marginTop = '10px';
  if (!e.passthrough.length) {
    list.innerHTML = '<p class="hint">No ranges marked.</p>';
  }
  e.passthrough.forEach((r, i) => {
    const row = document.createElement('div');
    row.className = 'row'; row.style.marginBottom = '6px';

    const lab = document.createElement('input');
    lab.value = r.label || 'intro'; lab.style.width = '110px';
    lab.oninput = () => { r.label = lab.value; saveLocal(); };

    const a = document.createElement('input');
    a.className = 'mono'; a.style.width = '110px'; a.value = fmt(r.t0);
    a.onchange = () => { r.t0 = parseTime(a.value, r.t0); a.value = fmt(r.t0);
                         saveLocal(); renderEdits(); };
    const b = document.createElement('input');
    b.className = 'mono'; b.style.width = '110px'; b.value = fmt(r.t1);
    b.onchange = () => { r.t1 = parseTime(b.value, r.t1); b.value = fmt(r.t1);
                         saveLocal(); renderEdits(); };

    const del = document.createElement('button');
    del.className = 'btn sm danger'; del.textContent = '✕';
    del.onclick = () => { e.passthrough.splice(i, 1); saveLocal(); renderEdits(); };

    const snip = { ...listenOpts(e.probe), name: e.name, snippet: true };
    row.append(lab,
      Object.assign(document.createElement('span'), { className: 'hint', textContent: 'from' }), a,
      playBtn(e.path, e.stream, r.t0 - 2, 6, '▶ start', snip),
      Object.assign(document.createElement('span'), { className: 'hint', textContent: 'to' }), b,
      playBtn(e.path, e.stream, r.t1 - 3, 6, '▶ end', snip),
      Object.assign(document.createElement('span'), {
        className: 'hint mono', textContent: `${(r.t1 - r.t0).toFixed(1)}s` }),
      del);
    list.append(row);
  });

  const add = document.createElement('button');
  add.className = 'btn sm'; add.textContent = '+ Add range';
  add.onclick = () => {
    const last = e.passthrough.at(-1);
    const t0 = last ? last.t1 + 1 : 0;
    e.passthrough.push({ t0, t1: t0 + 90, label: last ? 'passthrough' : 'intro' });
    saveLocal(); renderEdits();
  };
  list.append(add);
  box.append(list);
  return box;
}

/* Zoomed all the way in, one pixel is worth this many seconds at a typical
 * window width — past it the envelope is a flat line and only the readout
 * moves, so there is nothing left to see. */
const WAVE_MIN_SPAN = 0.5;

function waveCanvas(e) {
  const wrap = document.createElement('div');
  const cv = document.createElement('canvas');
  cv.className = 'wave';
  wrap.append(cv);

  const info = document.createElement('div');
  info.className = 'row'; info.style.marginTop = '6px';
  wrap.append(info);

  const dur = e.wave.duration;

  /* Zoom and selection live on the edit, not in this closure: marking a range
   * re-renders every card from scratch, and losing your zoom every time you
   * added one would make the zoom useless for setting the next one. */
  if (!e.waveView || !(e.waveView.t1 > e.waveView.t0) || e.waveView.t1 > dur + 0.01) {
    e.waveView = { t0: 0, t1: dur };
  }
  const view = e.waveView;
  let sel = e.waveSel || null;
  let dragging = false, panning = false, panFrom = 0;

  const span = () => view.t1 - view.t0;
  const zoomed = () => span() < dur - 0.01;

  /** Keep the window inside the file, whatever the caller did to it. */
  const clampView = (t0, s) => {
    const w = Math.max(WAVE_MIN_SPAN, Math.min(s, dur));
    view.t0 = Math.max(0, Math.min(t0, dur - w));
    view.t1 = view.t0 + w;
  };

  const draw = () => {
    const rect = cv.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    cv.width = rect.width * dpr; cv.height = 88 * dpr;
    const g = cv.getContext('2d');
    if (!g) return;           // no canvas: the numeric controls still work
    g.scale(dpr, dpr);
    const W = rect.width, H = 88;
    const xOf = t => (t - view.t0) / span() * W;
    g.clearRect(0, 0, W, H);

    // One column per pixel, taking the loudest envelope point it covers, so
    // zooming out never hides a transient by sampling past it.
    const pts = e.wave.points, n = pts.length;
    const perSec = n / dur;
    g.fillStyle = '#2f81f7';
    for (let x = 0; x < W; x++) {
      const a = Math.floor((view.t0 + x / W * span()) * perSec);
      const b = Math.max(a + 1, Math.ceil((view.t0 + (x + 1) / W * span()) * perSec));
      let v = 0;
      for (let i = Math.max(0, a); i < Math.min(n, b); i++) if (pts[i] > v) v = pts[i];
      const h = Math.max(1, v * (H - 14));
      g.fillRect(x, 6 + (H - 14 - h) / 2, 1, h);
    }

    for (const r of e.passthrough) {
      const x0 = xOf(r.t0), x1 = xOf(r.t1);
      if (x1 < 0 || x0 > W) continue;
      g.fillStyle = 'rgba(210,153,34,.28)';
      g.fillRect(x0, 5, Math.max(1, x1 - x0), H - 5);
      g.fillStyle = '#d29922';
      g.fillRect(x0, 5, 1, H - 5); g.fillRect(x1 - 1, 5, 1, H - 5);
      g.font = '10px ui-monospace,monospace';
      g.fillText(r.label || 'passthrough', x0 + 4, 16);
    }

    if (sel) {
      const x0 = xOf(Math.min(sel.a, sel.b)), x1 = xOf(Math.max(sel.a, sel.b));
      g.fillStyle = 'rgba(88,166,255,.25)';
      g.fillRect(x0, 5, x1 - x0, H - 5);
      g.fillStyle = '#58a6ff';
      g.fillRect(x0, 5, 1, H - 5); g.fillRect(x1 - 1, 5, 1, H - 5);
    }

    // Where the window sits in the whole file, so a zoomed view still says
    // which part of the episode you are looking at.
    g.fillStyle = '#21262d'; g.fillRect(0, 0, W, 4);
    g.fillStyle = zoomed() ? '#58a6ff' : '#30363d';
    g.fillRect(view.t0 / dur * W, 0, Math.max(2, span() / dur * W), 4);

    g.fillStyle = '#8b949e';
    g.font = '10px ui-monospace,monospace';
    for (let i = 0; i <= 6; i++) {
      const x = i / 6 * W;
      const t = view.t0 + i / 6 * span();
      // Sub-minute windows need the seconds' decimals to be worth reading.
      const lbl = span() < 20 ? fmt(t).slice(0, 8) : fmt(t).slice(0, 5);
      g.fillText(lbl, Math.min(W - 44, x + 2), H - 3);
    }
  };

  const timeAt = ev => {
    const rect = cv.getBoundingClientRect();
    const t = view.t0 + (ev.clientX - rect.left) / rect.width * span();
    return Math.max(0, Math.min(dur, t));
  };

  // Shift+wheel zooms about the pointer. Plain wheel is left alone so the page
  // still scrolls — the canvas is in the middle of a long form.
  cv.addEventListener('wheel', ev => {
    if (!ev.shiftKey) return;
    ev.preventDefault();
    // Browsers report shift+wheel as horizontal on some platforms.
    const d = ev.deltaY || ev.deltaX;
    if (!d) return;
    const focus = timeAt(ev);
    const s = span() * Math.exp(d * 0.0015);
    const frac = (focus - view.t0) / span();      // hold the pointer's time still
    clampView(focus - frac * Math.max(WAVE_MIN_SPAN, Math.min(s, dur)), s);
    draw(); renderInfo();
  }, { passive: false });

  cv.onmousedown = ev => {
    // Alt or middle button pans; anything else selects.
    if (ev.altKey || ev.button === 1) {
      ev.preventDefault();
      panning = true; panFrom = timeAt(ev);
      cv.style.cursor = 'grabbing';
      return;
    }
    if (ev.button !== 0) return;
    dragging = true;
    const t = timeAt(ev);
    sel = { a: t, b: t }; e.waveSel = sel;
    draw();
  };
  cv.onmousemove = ev => {
    if (panning) {
      clampView(view.t0 - (timeAt(ev) - panFrom), span());
      draw(); renderInfo();
      return;
    }
    if (dragging) { sel.b = timeAt(ev); draw(); renderInfo(); }
  };
  window.addEventListener('mouseup', () => {
    if (panning) { panning = false; cv.style.cursor = ''; }
    if (!dragging) return;
    dragging = false;
    // A click, not a drag: clear rather than leave a zero-width selection.
    if (sel && Math.abs(sel.b - sel.a) < span() / 400) sel = null;
    e.waveSel = sel;
    draw(); renderInfo();
  }, { signal: waveHandlers.signal });

  function renderInfo() {
    info.innerHTML = '';

    const zoom = document.createElement('span');
    zoom.className = 'hint mono';
    zoom.textContent = zoomed()
      ? `${fmt(view.t0).slice(0, 5)}–${fmt(view.t1).slice(0, 5)} · ${span().toFixed(1)}s wide`
      : `whole file · ${fmt(dur).slice(0, 5)}`;
    info.append(zoom);

    if (zoomed()) {
      const fit = document.createElement('button');
      fit.className = 'btn sm'; fit.textContent = 'Fit';
      fit.onclick = () => { clampView(0, dur); draw(); renderInfo(); };
      info.append(fit);
    }

    if (!sel) {
      info.append(Object.assign(document.createElement('span'), {
        className: 'hint',
        textContent: 'Drag across the theme to select it. Shift+scroll zooms, ' +
                     'alt+drag pans.' }));
      return;
    }
    const t0 = Math.min(sel.a, sel.b), t1 = Math.max(sel.a, sel.b);
    info.append(Object.assign(document.createElement('span'), {
      className: 'mono', textContent: `${fmt(t0)} → ${fmt(t1)} (${(t1 - t0).toFixed(1)}s)` }));
    const snip = { ...listenOpts(e.probe), name: e.name, snippet: true };
    info.append(playBtn(e.path, e.stream, t0 - 1.5, 6, '▶ at start', snip));
    info.append(playBtn(e.path, e.stream, t1 - 4.5, 6, '▶ at end', snip));
    const b = document.createElement('button');
    b.className = 'btn sm primary'; b.textContent = 'Mark as passthrough';
    b.onclick = () => {
      e.passthrough.push({ t0, t1, label: e.passthrough.length ? 'passthrough' : 'intro' });
      sel = null; e.waveSel = null; saveLocal(); renderEdits();
    };
    info.append(b);
  }

  requestAnimationFrame(() => { draw(); renderInfo(); });
  window.addEventListener('resize', draw, { signal: waveHandlers.signal });
  return wrap;
}

// ─────────────────────────── step 3: run ────────────────────────────

function buildPlan(e) {
  const sources = e.sources.map(id => {
    const p = S.pairs.find(x => x.id === id);
    const s = { label: p.label,
                jpn: { path: p.jpn.path, audio_stream: p.jpn.stream } };
    if (p.dub && p.dub.stream != null) {
      s.dub = { path: p.dub.path, audio_stream: p.dub.stream };
      if (p.offset != null) s.dub_offset = p.offset;
    }
    return s;
  });
  return {
    edit: { path: e.path, audio_stream: e.stream, duration: e.probe?.duration || 0 },
    sources,
    passthrough: e.passthrough.map(r => ({ t0: r.t0, t1: r.t1, label: r.label || 'passthrough' })),
  };
}

function selectedEdits() { return S.edits.filter(e => e.selected); }

$('#btnValidate').onclick = async () => {
  const eds = selectedEdits();
  const out = $('#validateOut');
  if (!eds.length) { out.innerHTML = '<p class="hint bad">No edits selected.</p>'; return; }

  const sep = '<hr style="border:0;border-top:1px solid var(--border);margin:6px 0">';

  const blocking = [];
  for (const e of eds) {
    const probs = editProblems(e);
    if (probs.length) blocking.push(`<b>${esc(e.name)}</b><br>` +
      probs.map(p => `· ${esc(p)}`).join('<br>'));
  }
  if (blocking.length) {
    out.innerHTML = `<div class="note warn">${blocking.join(sep)}</div>`;
    return;
  }

  const warns = [];
  for (const e of eds) {
    const w = editWarnings(e);
    if (w.length) warns.push(`<b>${esc(e.name)}</b><br>` +
      w.map(x => `· ${esc(x)}`).join('<br>'));
  }
  const warnHtml = warns.length
    ? `<div class="note warn">These will not stop the run:${sep}${warns.join(sep)}</div>`
    : '';

  out.innerHTML = warnHtml + '<p class="hint">Checking every file and stream…</p>';
  try {
    const r = await api('/api/validate', {
      method: 'POST', body: JSON.stringify({ plans: eds.map(buildPlan) }) });
    out.innerHTML = warnHtml + r.results.map(x => x.ok
      ? `<div class="row"><span class="pill ok">ok</span> <span class="hint">${esc(x.edit)}</span></div>`
      : `<div class="row"><span class="pill bad">problem</span> <span class="hint bad">${esc(x.error)}</span></div>`
    ).join('');
  } catch (err) { out.innerHTML = `<p class="hint bad">${esc(err.message)}</p>`; }
};

$('#btnRun').onclick = async () => {
  const eds = selectedEdits();
  if (!eds.length) { toast('No edits selected', 'bad'); return; }
  const blocked = eds.filter(e => editProblems(e).length);
  if (blocked.length) {
    toast(`${blocked.length} edit(s) still need input — see step 2`, 'bad');
    return;
  }
  try {
    const r = await api('/api/jobs', {
      method: 'POST',
      body: JSON.stringify({
        plans: eds.map(buildPlan),
        mux: $('#optMux').checked,
        crossfade: parseFloat($('#optCrossfade').value) || 0,
        dub_lang: $('#optLang').value.trim() || 'eng',
      }),
    });
    toast(`Queued ${r.jobs.length} job(s)`, 'ok');
    if (r.jobs.length) S.activeJob = r.jobs[0].id;
    await loadJobs();
  } catch (e) { toast(e.message, 'bad', 9000); }
};

async function loadJobs() {
  try {
    S.jobs = (await api('/api/jobs')).jobs;
    renderJobs();
  } catch (_) {}
}

function renderJobs() {
  const host = $('#jobList');
  const active = S.jobs.filter(j => j.state === 'queued' || j.state === 'running');
  $('#nQueue').textContent = active.length;

  if (!S.jobs.length) { host.innerHTML = '<p class="empty">No jobs yet.</p>'; return; }
  host.innerHTML = '';

  for (const j of S.jobs.slice(0, 40)) {
    const row = document.createElement('div');
    row.className = 'card tight';
    row.style.cssText = 'margin-bottom:8px;cursor:pointer' +
      (j.id === S.activeJob ? ';border-color:var(--accent)' : '');
    row.onclick = () => { S.activeJob = j.id; loadJobLog(j.id); renderJobs(); };

    const cls = { done: 'ok', failed: 'bad', running: 'info',
                  cancelled: 'warn', queued: '' }[j.state] || '';
    const top = document.createElement('div');
    top.className = 'row';
    top.innerHTML = `<span class="pill ${cls}">${j.state}</span>` +
      `<b style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;` +
      `white-space:nowrap">${esc(j.title)}</b>` +
      `<span class="hint mono">${esc(j.stage)}</span>`;
    row.append(top);

    const bar = document.createElement('div');
    bar.className = 'bar' + (j.state === 'done' ? ' done' : j.state === 'failed' ? ' failed' : '');
    bar.style.marginTop = '7px';
    bar.innerHTML = `<i style="width:${j.state === 'done' ? 100 : j.pct}%"></i>`;
    row.append(bar);

    if (j.error) row.append(Object.assign(document.createElement('p'),
      { className: 'hint bad', style: 'margin-top:6px', textContent: j.error }));

    const outs = Object.entries(j.outputs || {})
      .filter(([k]) => k === 'wav' || k === 'mkv');
    if (outs.length) {
      const dl = document.createElement('div');
      dl.className = 'row'; dl.style.marginTop = '7px';
      for (const [k, v] of outs) {
        const a = document.createElement('a');
        a.href = `/api/download?path=${encodeURIComponent(v)}`;
        a.className = 'pill info'; a.textContent = `↓ ${k}`;
        a.onclick = ev => ev.stopPropagation();
        dl.append(a);
      }
      row.append(dl);
    }
    host.append(row);
  }
}

async function loadJobLog(id) {
  try {
    const j = await api(`/api/jobs/${id}`);
    $('#logTitle').textContent = j.title;
    const el = $('#jobLog');
    el.innerHTML = (j.lines || []).map(l => {
      const c = /^ERROR|failed/i.test(l) ? 'err'
        : /WARNING/.test(l) ? 'warn' : l.startsWith('$') ? 'cmd' : '';
      return c ? `<span class="${c}">${esc(l)}</span>` : esc(l);
    }).join('\n');
    el.scrollTop = el.scrollHeight;
    $('#btnCancelJob').style.display =
      (j.state === 'running' || j.state === 'queued') ? '' : 'none';
  } catch (_) {}
}

$('#btnCancelJob').onclick = async () => {
  if (!S.activeJob) return;
  try { await api(`/api/jobs/${S.activeJob}/cancel`, { method: 'POST' }); }
  catch (e) { toast(e.message, 'bad'); }
};

function connectEvents() {
  const es = new EventSource('/api/events');
  es.onmessage = ev => {
    let d; try { d = JSON.parse(ev.data); } catch (_) { return; }
    if (d.type === 'queue') { loadJobs(); return; }
    if (d.type !== 'job') return;

    const i = S.jobs.findIndex(j => j.id === d.job.id);
    if (i >= 0) S.jobs[i] = { ...S.jobs[i], ...d.job }; else S.jobs.unshift(d.job);
    renderJobs();

    if (d.id === S.activeJob && d.line) {
      const el = $('#jobLog');
      const near = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
      const c = /^ERROR|failed/i.test(d.line) ? 'err'
        : /WARNING/.test(d.line) ? 'warn' : d.line.startsWith('$') ? 'cmd' : '';
      el.insertAdjacentHTML('beforeend',
        (el.textContent ? '\n' : '') + (c ? `<span class="${c}">${esc(d.line)}</span>` : esc(d.line)));
      if (near) el.scrollTop = el.scrollHeight;
    }
    if (d.job.state === 'done' && d.job.outputs?.edl) loadEdlList();
    if (d.id === S.activeJob) {
      $('#btnCancelJob').style.display =
        (d.job.state === 'running' || d.job.state === 'queued') ? '' : 'none';
    }
  };
  es.onerror = () => { es.close(); setTimeout(connectEvents, 3000); };
}

// ────────────────────────── step 4: review ──────────────────────────

async function loadEdlList() {
  try {
    const r = await api('/api/edls');
    const sel = $('#edlPick');
    const cur = sel.value;
    sel.innerHTML = '<option value="">— pick an EDL —</option>' +
      r.edls.map(e => `<option value="${esc(e.path)}">${esc(e.name)} · ` +
        `${e.segments} segments${e.passthrough ? ` · ${e.passthrough} passthrough` : ''}` +
        `</option>`).join('');
    if (cur) sel.value = cur;
  } catch (_) {}
}

$('#btnReloadEdls').onclick = loadEdlList;
$('#edlPick').onchange = () => { if ($('#edlPick').value) openEdl($('#edlPick').value); };

async function openEdl(path) {
  try {
    S.edl = await api(`/api/edl?path=${encodeURIComponent(path)}`);
    S.edlPath = path; S.edlDirty = false; S.segIdx = null;
    $('#reviewBody').style.display = '';
    renderReview();
  } catch (e) { toast(e.message, 'bad'); }
}

function segFlag(s) {
  if (s.status === 'passthrough') return null;
  if (s.src == null) return 'gap';
  if (s.conf != null && s.conf < LOW_CONF) return 'low confidence';
  return null;
}

function renderReview() {
  const d = S.edl;
  $('#edlTitle').textContent = d.edit || '';
  const segs = d.segments || [];
  const flagged = segs.filter(segFlag).length;
  const pt = segs.filter(s => s.status === 'passthrough').length;
  $('#edlStats').innerHTML =
    `${segs.length} segments · ${fmt(d.duration)}` +
    (pt ? ` · <span class="pill warn">${pt} passthrough</span>` : '') +
    (flagged ? ` · <span class="pill bad">${flagged} flagged</span>`
             : ` · <span class="pill ok">none flagged</span>`);
  $('#edlDirty').textContent = S.edlDirty ? 'unsaved changes' : '';
  $('#edlDirty').className = S.edlDirty ? 'hint warn' : 'hint';

  drawTimeline();
  renderLegend();
  renderSegList();
  renderInspector();
}

function renderLegend() {
  const host = $('#tlLegend');
  host.innerHTML = '';
  (S.edl.sources || []).forEach((name, i) => {
    const s = document.createElement('span');
    s.innerHTML = `<i style="background:${srcColor(i)}"></i>` +
      `<span class="hint">${esc(name)}</span>`;
    host.append(s);
  });
  const p = document.createElement('span');
  p.innerHTML = `<i style="background:${PASSTHROUGH_COLOR}"></i>` +
    `<span class="hint">passthrough (edit's own audio)</span>`;
  host.append(p);
}

function drawTimeline() {
  const cv = $('#tlCanvas');
  const rect = cv.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  cv.width = rect.width * dpr; cv.height = 56 * dpr;
  const g = cv.getContext('2d');
  // A drawing failure must not take the segment list and inspector with it —
  // renderReview() calls this first, and those are the views you actually fix
  // cuts from.
  if (!g) return;
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  const W = rect.width, H = 56, dur = S.edl.duration || 1;
  g.clearRect(0, 0, W, H);

  (S.edl.segments || []).forEach((s, i) => {
    const x0 = s.t0 / dur * W, x1 = s.t1 / dur * W;
    const w = Math.max(1, x1 - x0);
    g.fillStyle = s.status === 'passthrough' ? PASSTHROUGH_COLOR
      : s.src == null ? '#484f58' : srcColor(s.src);
    g.fillRect(x0, 8, w, 30);
    if (segFlag(s)) {
      g.fillStyle = '#f85149';
      g.fillRect(x0, 38, w, 4);
    }
    if (i === S.segIdx) {
      g.strokeStyle = '#fff'; g.lineWidth = 2;
      g.strokeRect(x0, 7, w, 32);
    }
  });

  g.fillStyle = '#8b949e';
  g.font = '10px ui-monospace,monospace';
  for (let i = 0; i <= 8; i++) {
    const x = i / 8 * W;
    g.fillRect(x, 44, 1, 4);
    g.fillText(fmt(i / 8 * dur).slice(0, 5), Math.min(W - 30, x + 2), H - 1);
  }

  cv.onclick = ev => {
    const r = cv.getBoundingClientRect();
    const t = (ev.clientX - r.left) / r.width * dur;
    const i = (S.edl.segments || []).findIndex(s => t >= s.t0 && t < s.t1);
    if (i >= 0) { S.segIdx = i; renderReview(); }
  };
}

function renderSegList() {
  const only = $('#onlyProblems').checked;
  const host = $('#segList');
  const rows = (S.edl.segments || []).map((s, i) => ({ s, i }))
    .filter(({ s }) => !only || segFlag(s));

  if (!rows.length) { host.innerHTML = '<p class="empty">Nothing flagged.</p>'; return; }

  const t = document.createElement('table');
  t.innerHTML = '<thead><tr><th>#</th><th>Start</th><th>End</th><th>Source</th>' +
                '<th>Source time</th><th>Conf</th></tr></thead>';
  const tb = document.createElement('tbody');
  for (const { s, i } of rows) {
    const tr = document.createElement('tr');
    if (i === S.segIdx) tr.className = 'sel';
    const flag = segFlag(s);
    const srcName = s.status === 'passthrough'
      ? `<span class="pill warn">${esc(s.label || 'passthrough')}</span>`
      : s.src == null ? '<span class="pill bad">gap</span>'
      : `<span style="color:${srcColor(s.src)}">■</span> ${esc(
          (S.edl.sources || [])[s.src] || s.src)}`;
    tr.innerHTML =
      `<td class="mono">${i}</td>` +
      `<td class="mono">${fmt(s.t0)}</td>` +
      `<td class="mono">${fmt(s.t1)}</td>` +
      `<td style="max-width:230px;overflow:hidden;text-overflow:ellipsis;` +
      `white-space:nowrap">${srcName}</td>` +
      `<td class="mono">${s.src_t0 == null ? '—' : fmt(s.src_t0)}</td>` +
      `<td class="mono ${flag ? 'hint bad' : 'hint'}">` +
      `${s.conf == null ? '—' : Math.round(s.conf)}</td>`;
    tr.onclick = () => { S.segIdx = i; renderReview(); };
    tb.append(tr);
  }
  t.append(tb);
  host.innerHTML = ''; host.append(t);
}

function nudge(boundaryIdx, delta) {
  // boundaryIdx is the cut between segment i and i+1.
  const segs = S.edl.segments;
  const a = segs[boundaryIdx], b = segs[boundaryIdx + 1];
  if (!a || !b) return;

  const t = a.t1 + delta;
  // Never let a nudge invert a segment; 50 ms of headroom keeps it audible.
  if (t <= a.t0 + 0.05 || t >= b.t1 - 0.05) {
    toast('That would collapse a segment', 'bad');
    return;
  }
  a.t1 = t;
  // b keeps pointing at the same moment of its source: shifting its start by
  // delta shifts its source start by the same amount, so delta is preserved.
  if (b.src_t0 != null) b.src_t0 += delta;
  b.t0 = t;
  S.edlDirty = true;
  renderReview();
}

function renderInspector() {
  const host = $('#inspector');
  const i = S.segIdx;
  const segs = S.edl.segments || [];
  if (i == null || !segs[i]) {
    host.innerHTML = '<p class="empty">Pick a segment on the timeline or in the list.</p>';
    return;
  }
  const s = segs[i];
  host.innerHTML = '';

  const flag = segFlag(s);
  const head = document.createElement('div');
  head.className = 'row';
  head.innerHTML = `<b>Segment ${i}</b>` +
    (flag ? ` <span class="pill bad">${esc(flag)}</span>` : '') +
    (s.status === 'passthrough' ? ` <span class="pill warn">passthrough</span>` : '');
  host.append(head);

  const tbl = document.createElement('table');
  tbl.style.marginTop = '8px';
  const srcName = s.src == null ? '—' : ((S.edl.sources || [])[s.src] || s.src);
  tbl.innerHTML = `<tbody>
    <tr><td class="hint">Edit</td><td class="mono">${fmt(s.t0)} → ${fmt(s.t1)}
        <span class="hint">(${(s.t1 - s.t0).toFixed(3)}s)</span></td></tr>
    <tr><td class="hint">Source</td><td>${esc(srcName)}</td></tr>
    <tr><td class="hint">Source time</td><td class="mono">${
      s.src_t0 == null ? '—' : `${fmt(s.src_t0)} → ${fmt(s.src_t0 + (s.t1 - s.t0))}`}</td></tr>
    <tr><td class="hint">Delta</td><td class="mono">${
      s.delta == null ? '—' : s.delta.toFixed(4) + ' s'}</td></tr>
    <tr><td class="hint">Confidence</td><td class="mono">${
      s.conf == null ? '—' : Math.round(s.conf)}</td></tr>
    <tr><td class="hint">Status</td><td class="mono">${esc(s.status || 'ok')}</td></tr>
  </tbody>`;
  host.append(tbl);

  // Listening: the edit's Japanese against the dub at the same source moment.
  if (S.edl.edit_path && S.edl.edit_stream != null) {
    const h = document.createElement('h3');
    h.style.marginTop = '12px'; h.textContent = 'Hear this segment';
    host.append(h);

    const row = document.createElement('div');
    row.className = 'row'; row.style.marginTop = '6px';
    row.append(Object.assign(document.createElement('span'),
      { className: 'hint', textContent: 'edit (jpn)' }));
    const editSnip = { duration: S.edl.duration || 0, snippet: true };
    row.append(playBtn(S.edl.edit_path, S.edl.edit_stream, s.t0, 6, '▶ start', editSnip));
    row.append(playBtn(S.edl.edit_path, S.edl.edit_stream, s.t1 - 5, 6, '▶ end', editSnip));

    const dubPath = (S.edl.dub_paths || {})[String(s.src)];
    const dubStream = (S.edl.dub_streams || {})[String(s.src)];
    if (dubPath && dubStream != null && s.src_t0 != null) {
      const off = (S.edl.dub_offsets || {})[String(s.src)] || 0;
      row.append(Object.assign(document.createElement('span'),
        { className: 'hint', style: 'margin-left:8px', textContent: 'dub' }));
      row.append(playBtn(dubPath, dubStream, s.src_t0 - off, 6, '▶ start', { snippet: true }));
      row.append(playBtn(dubPath, dubStream,
        s.src_t0 - off + (s.t1 - s.t0) - 5, 6, '▶ end', { snippet: true }));
    }
    host.append(row);
    host.append(Object.assign(document.createElement('p'), {
      className: 'hint', style: 'margin-top:5px',
      textContent: 'The same scene should be playing in both languages.' }));
  }

  if (i < segs.length - 1) {
    const h = document.createElement('h3');
    h.style.marginTop = '14px';
    h.textContent = `Cut at ${fmt(s.t1)} (between ${i} and ${i + 1})`;
    host.append(h);

    const row = document.createElement('div');
    row.className = 'row'; row.style.marginTop = '6px';
    for (const d of [-0.5, -0.1, -0.01, 0.01, 0.1, 0.5]) {
      const b = document.createElement('button');
      b.className = 'btn sm';
      b.textContent = `${d > 0 ? '+' : ''}${(d * 1000).toFixed(0)} ms`;
      b.onclick = () => nudge(i, d);
      row.append(b);
    }
    host.append(row);

    const exact = document.createElement('div');
    exact.className = 'row'; exact.style.marginTop = '8px';
    const inp = document.createElement('input');
    inp.className = 'mono'; inp.value = fmt(s.t1); inp.style.width = '120px';
    const go = document.createElement('button');
    go.className = 'btn sm'; go.textContent = 'Set cut';
    go.onclick = () => nudge(i, parseTime(inp.value, s.t1) - s.t1);
    exact.append(Object.assign(document.createElement('span'),
      { className: 'hint', textContent: 'exact' }), inp, go);
    host.append(exact);

    host.append(Object.assign(document.createElement('p'), {
      className: 'hint', style: 'margin-top:6px',
      textContent: 'Moving a cut moves the next segment\'s source time with it, ' +
                   'so its content stays put.' }));
  }
}

$('#onlyProblems').onchange = renderSegList;

$('#btnSaveEdl').onclick = () => saveEdl();

async function saveEdl() {
  if (!S.edlPath) return false;
  try {
    await api('/api/edl', {
      method: 'POST',
      body: JSON.stringify({ path: S.edlPath, edl: S.edl }) });
    S.edlDirty = false;
    $('#edlDirty').textContent = 'saved';
    toast('EDL saved (previous version kept as .bak)', 'ok');
    return true;
  } catch (e) { toast(`Save failed: ${e.message}`, 'bad', 9000); return false; }
}

$('#btnRerender').onclick = async () => {
  if (!await saveEdl()) return;
  try {
    const j = await api('/api/rerender', {
      method: 'POST',
      body: JSON.stringify({
        edl_path: S.edlPath,
        mux: $('#optMux').checked,
        crossfade: parseFloat($('#optCrossfade').value) || 0,
        dub_lang: $('#optLang').value.trim() || 'eng' }),
    });
    S.activeJob = j.id;
    toast('Re-render queued — alignment is not re-run', 'ok');
    showStep('run');
    await loadJobs();
  } catch (e) { toast(e.message, 'bad'); }
};

// ───────────────────────────── projects ─────────────────────────────

$('#btnSaveProject').onclick = async () => {
  const name = prompt('Save this setup as:', S.projectName || 'post-enies-lobby');
  if (!name) return;
  try {
    await api('/api/projects', {
      method: 'POST',
      body: JSON.stringify({ name, data: {
        pairs: S.pairs.map(p => ({ ...p, jpn: p.jpn && { ...p.jpn, probe: null },
                                   dub: p.dub && { ...p.dub, probe: null } })),
        edits: S.edits.map(e => ({ ...e, probe: null, wave: null })),
        editDir: S.editDir, sourceDir: S.sourceDir, sourceMode: S.sourceMode,
      } }),
    });
    S.projectName = name;
    $('#projectLabel').textContent = name;
    saveLocal();
    toast(`Saved setup "${name}"`, 'ok');
  } catch (e) { toast(e.message, 'bad'); }
};

$('#btnLoadProject').onclick = async () => {
  try {
    const r = await api('/api/projects');
    if (!r.projects.length) { toast('No saved setups', 'bad'); return; }
    const name = prompt('Load which setup?\n\n' +
      r.projects.map(p => `· ${p.name} (${p.sources} sources)`).join('\n'),
      r.projects[0].name);
    if (!name) return;
    const d = await api(`/api/project?name=${encodeURIComponent(name)}`);
    S.pairs = d.pairs || [];
    S.edits = [];
    if (d.sourceMode) S.sourceMode = d.sourceMode;
    if (d.editDir) setFolder('edits', d.editDir);
    if (d.sourceDir) setFolder('sources', d.sourceDir);
    for (const e of (d.edits || [])) {
      try { e.probe = await probeOne(e.path); } catch (_) { e.probe = null; }
      e.wave = null;
      S.edits.push(e);
    }
    S.projectName = name;
    $('#projectLabel').textContent = name;
    // Re-probes the sources folder, which re-attaches probe data to the pairs.
    if (S.sourceDir) await $('#btnLoadSources').onclick();
    saveLocal(); renderPairs(); renderEdits();
    toast(`Loaded "${name}"`, 'ok');
  } catch (e) { toast(e.message, 'bad'); }
};

// ─────────────────────────────── boot ───────────────────────────────

function showStep(name) {
  $$('nav button').forEach(b => b.classList.toggle('active', b.dataset.step === name));
  $$('section.step').forEach(s => s.classList.toggle('active', s.id === `step-${name}`));
  if (name === 'review') loadEdlList();
  if (name === 'review' && S.edl) requestAnimationFrame(drawTimeline);
}
$$('nav button').forEach(b => { b.onclick = () => showStep(b.dataset.step); });

window.addEventListener('resize', () => {
  if ($('#step-review').classList.contains('active') && S.edl) drawTimeline();
});

(async function boot() {
  try {
    S.config = await api('/api/config');
  } catch (e) {
    document.body.innerHTML =
      `<p style="padding:30px;color:#f85149">Cannot reach the server: ${esc(e.message)}</p>`;
    return;
  }
  $('#rootsLabel').textContent =
    S.config.roots.map(r => r.path + (r.exists ? '' : ' (missing)')).join('  ·  ') +
    `   →   ${S.config.out}`;
  await restoreLocal();
  if (S.projectName) $('#projectLabel').textContent = S.projectName;
  renderPairs(); renderEdits();
  await loadFolders();

  // Default to the conventional layout — <root>/sources and <root>/edits —
  // so both tabs open on the right folder without anyone typing a path.
  setFolder('sources', S.sourceDir || defaultFolder('sources'));
  setFolder('edits', S.editDir || defaultFolder('edits'));

  // Loading the sources folder fills the file pool and re-attaches probe data
  // to pairs restored from storage, which is what the drag-and-drop and the
  // track lists are drawn from. Server-side probe caching keeps it cheap.
  if (S.sourceDir) await $('#btnLoadSources').onclick();
  if (S.editDir) await $('#btnLoadEdits').onclick();

  await loadJobs(); await loadEdlList();
  connectEvents();
})();
