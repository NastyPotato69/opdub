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
  pairs: [],      // source episodes, hand-paired
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

function stopAudio() {
  if (currentAudio) { currentAudio.pause(); currentAudio = null; }
  if (currentBtn) { currentBtn.classList.remove('playing'); currentBtn = null; }
}

function play(path, stream, t, dur, btn) {
  const same = currentBtn === btn;
  stopAudio();
  if (same) return;
  const url = `/api/preview?path=${encodeURIComponent(path)}&stream=${stream}` +
              `&t=${Math.max(0, t).toFixed(3)}&dur=${dur}`;
  const a = new Audio(url);
  currentAudio = a; currentBtn = btn;
  if (btn) btn.classList.add('playing');
  a.onended = stopAudio;
  a.onerror = () => { toast('Could not play that stream', 'bad'); stopAudio(); };
  a.play().catch(() => {});
}

/** A listen button. Defaults to mid-file, which is where speech lives —
 *  the opening theme is identical in every language and proves nothing. */
function playBtn(path, stream, t, dur = 6, label = '▶') {
  const b = document.createElement('button');
  b.className = 'play'; b.textContent = label; b.title = `Listen at ${fmt(t)}`;
  b.onclick = e => { e.stopPropagation(); play(path, stream, t, dur, b); };
  return b;
}

// ─────────────────────────── persistence ────────────────────────────

function saveLocal() {
  try {
    localStorage.setItem('opdub', JSON.stringify({
      pairs: S.pairs,
      edits: S.edits.map(e => ({ ...e, probe: null, wave: null })),
      editDir: $('#editDir').value, projectName: S.projectName,
    }));
  } catch (_) {}
}

async function restoreLocal() {
  let d;
  try { d = JSON.parse(localStorage.getItem('opdub') || 'null'); } catch (_) { return; }
  if (!d) return;
  S.pairs = d.pairs || [];
  S.projectName = d.projectName || '';
  if (d.editDir) $('#editDir').value = d.editDir;
  for (const e of (d.edits || [])) {
    try { e.probe = (await api('/api/probe', {
      method: 'POST', body: JSON.stringify({ paths: [e.path] }) })).files[0];
    } catch (_) { e.probe = null; }
    S.edits.push(e);
  }
}

// ────────────────────────── file picker ─────────────────────────────

let pickResolve = null;

async function pickFile(title, startDir) {
  $('#pickTitle').textContent = title;
  $('#pickDir').value = startDir || S.config.roots[0]?.path || '';
  await browseInto($('#pickDir').value);
  $('#pickDlg').showModal();
  return new Promise(res => { pickResolve = res; });
}

async function browseInto(dir) {
  const list = $('#pickList');
  list.innerHTML = '<p class="empty">Loading…</p>';
  try {
    const d = await api(`/api/browse?dir=${encodeURIComponent(dir)}`);
    $('#pickDir').value = d.dir;
    list.innerHTML = '';

    const roots = document.createElement('div');
    roots.className = 'row'; roots.style.marginBottom = '10px';
    for (const r of S.config.roots) {
      const b = document.createElement('button');
      b.className = 'btn sm'; b.textContent = r.path;
      b.onclick = () => browseInto(r.path);
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
      tr.onclick = () => { $('#pickDlg').close(); pickResolve?.(f.path); pickResolve = null; };
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

$('#pickGo').onclick = () => browseInto($('#pickDir').value);
$('#pickClose').onclick = () => { $('#pickDlg').close(); pickResolve?.(null); pickResolve = null; };
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
    row.append(playBtn(pr.path, a.index, mid, 6, '▶ listen'));
    box.append(row);
  }
  if (!pr.audio.length) box.innerHTML = '<p class="hint bad">No audio streams.</p>';
  return box;
}

// ───────────────────────── step 1: sources ──────────────────────────

function renderPairs() {
  const host = $('#pairTable');
  host.innerHTML = '';
  $('#nSources').textContent = S.pairs.length;

  if (!S.pairs.length) {
    host.innerHTML = '<p class="empty">No source episodes yet. ' +
      'Add one for each original episode your edits draw from.</p>';
    return;
  }

  for (const p of S.pairs) {
    const card = document.createElement('div');
    card.className = 'card tight';
    card.style.marginBottom = '10px';

    const head = document.createElement('div');
    head.className = 'row';
    const name = document.createElement('input');
    name.value = p.label; name.style.flex = '1'; name.style.minWidth = '180px';
    name.oninput = () => { p.label = name.value; saveLocal(); refreshEditCards(); };
    head.append(Object.assign(document.createElement('span'),
      { className: 'pill info', textContent: `#${S.pairs.indexOf(p)}` }), name);

    const del = document.createElement('button');
    del.className = 'btn sm danger'; del.textContent = 'Remove';
    del.onclick = () => {
      S.pairs = S.pairs.filter(x => x.id !== p.id);
      for (const e of S.edits) e.sources = e.sources.filter(id => id !== p.id);
      saveLocal(); renderPairs(); refreshEditCards();
    };
    const sp = document.createElement('span'); sp.style.flex = '1';
    head.append(sp, del);
    card.append(head);

    const grid = document.createElement('div');
    grid.className = 'grid2'; grid.style.marginTop = '10px';
    grid.append(slotUI(p, 'jpn', 'Japanese track — used for alignment'),
                slotUI(p, 'dub', 'Dub track — used for the new audio'));
    card.append(grid);
    card.append(offsetUI(p));
    host.append(card);
  }
}

function slotUI(pair, key, title) {
  const box = document.createElement('div');
  const slot = pair[key];

  const h = document.createElement('h3');
  h.textContent = title;
  box.append(h);

  const row = document.createElement('div');
  row.className = 'row'; row.style.margin = '6px 0';
  const btn = document.createElement('button');
  btn.className = 'btn sm';
  btn.textContent = slot?.path ? 'Change file' : 'Choose file…';
  btn.onclick = async () => {
    const path = await pickFile(title, slot?.path
      ? slot.path.replace(/\/[^/]+$/, '') : undefined);
    if (!path) return;
    const pr = await probeOne(path);
    // No stream is preselected: picking one is the operator's call.
    pair[key] = { path, name: pr.name, stream: null, probe: pr };
    pair.offset = null; pair.offsetReport = null;
    saveLocal(); renderPairs();
  };
  row.append(btn);
  if (slot?.path) {
    row.append(Object.assign(document.createElement('span'),
      { className: 'mono hint', textContent: slot.name,
        style: 'flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' }));
  }
  box.append(row);

  if (slot?.path) {
    if (!slot.probe) {
      probeOne(slot.path).then(pr => { slot.probe = pr; renderPairs(); });
      box.append(Object.assign(document.createElement('p'),
        { className: 'hint', textContent: 'Probing…' }));
    } else {
      box.append(streamChooser(slot.probe, slot.stream, idx => {
        slot.stream = idx; pair.offset = null; pair.offsetReport = null;
        saveLocal(); renderPairs();
      }));
      if (slot.stream == null) {
        box.append(Object.assign(document.createElement('p'), {
          className: 'hint warn', style: 'margin-top:6px',
          textContent: '↑ Listen, then pick the stream. Nothing is chosen for you.',
        }));
      }
    }
  }
  return box;
}

function offsetUI(pair) {
  const box = document.createElement('div');
  box.style.marginTop = '10px';
  box.style.paddingTop = '10px';
  box.style.borderTop = '1px solid var(--border)';

  const ready = pair.jpn?.stream != null && pair.dub?.stream != null;
  const row = document.createElement('div');
  row.className = 'row';

  row.append(Object.assign(document.createElement('span'),
    { className: 'hint', textContent: 'jpn → dub offset' }));

  const inp = document.createElement('input');
  inp.type = 'number'; inp.step = '0.001'; inp.className = 'mono';
  inp.value = pair.offset != null ? pair.offset.toFixed(4) : '';
  inp.placeholder = 'measure or type';
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
      { className: 'hint', textContent: 'pick both streams first' }));
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
    const t = (pair.jpn.probe?.duration || 900) / 2;
    cmp.append(Object.assign(document.createElement('span'),
      { className: 'hint', textContent: 'Same scene?' }));
    const bj = playBtn(pair.jpn.path, pair.jpn.stream, t, 6, '▶ jpn');
    const bd = playBtn(pair.dub.path, pair.dub.stream,
      t - (pair.offset || 0), 6, '▶ dub');
    cmp.append(bj, bd);
    cmp.append(Object.assign(document.createElement('span'), {
      className: 'hint',
      textContent: 'Both should be the same moment in different languages.' }));
    box.append(cmp);
  }
  return box;
}

$('#btnAddPair').onclick = () => {
  S.pairs.push({ id: uid(), label: `Episode ${S.pairs.length + 1}`,
                 jpn: null, dub: null, offset: null, offsetReport: null });
  saveLocal(); renderPairs();
};

// ────────────────────────── step 2: edits ───────────────────────────

$('#btnLoadEdits').onclick = async () => {
  const dir = $('#editDir').value.trim();
  if (!dir) return;
  try {
    const d = await api(`/api/browse?dir=${encodeURIComponent(dir)}`);
    const known = new Set(S.edits.map(e => e.path));
    const probes = await api('/api/probe', {
      method: 'POST',
      body: JSON.stringify({ paths: d.files.map(f => f.path) }) });
    for (const pr of probes.files) {
      if (known.has(pr.path)) continue;
      S.edits.push({ path: pr.path, name: pr.name, probe: pr, stream: null,
                     sources: [], passthrough: [], selected: false, wave: null });
    }
    saveLocal(); renderEdits();
    toast(`${d.files.length} file(s) in ${d.dir}`, 'ok');
  } catch (e) { toast(e.message, 'bad'); }
};

function refreshEditCards() { renderEdits(); }

function renderEdits() {
  const host = $('#editList');
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
        e.wave = await api(`/api/waveform?path=${encodeURIComponent(e.path)}` +
                           `&stream=${e.stream}&points=1400`);
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

    row.append(lab,
      Object.assign(document.createElement('span'), { className: 'hint', textContent: 'from' }), a,
      playBtn(e.path, e.stream, r.t0 - 2, 6, '▶ start'),
      Object.assign(document.createElement('span'), { className: 'hint', textContent: 'to' }), b,
      playBtn(e.path, e.stream, r.t1 - 3, 6, '▶ end'),
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

function waveCanvas(e) {
  const wrap = document.createElement('div');
  const cv = document.createElement('canvas');
  cv.className = 'wave';
  wrap.append(cv);

  const info = document.createElement('div');
  info.className = 'row'; info.style.marginTop = '6px';
  wrap.append(info);

  const dur = e.wave.duration;
  let sel = null, dragging = false;

  const draw = () => {
    const rect = cv.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    cv.width = rect.width * dpr; cv.height = 88 * dpr;
    const g = cv.getContext('2d');
    g.scale(dpr, dpr);
    const W = rect.width, H = 88;
    g.clearRect(0, 0, W, H);

    const pts = e.wave.points, n = pts.length;
    g.fillStyle = '#2f81f7';
    for (let x = 0; x < W; x++) {
      const v = pts[Math.min(n - 1, Math.floor(x / W * n))];
      const h = Math.max(1, v * (H - 8));
      g.fillRect(x, (H - h) / 2, 1, h);
    }

    for (const r of e.passthrough) {
      const x0 = r.t0 / dur * W, x1 = r.t1 / dur * W;
      g.fillStyle = 'rgba(210,153,34,.28)';
      g.fillRect(x0, 0, Math.max(1, x1 - x0), H);
      g.fillStyle = '#d29922';
      g.fillRect(x0, 0, 1, H); g.fillRect(x1 - 1, 0, 1, H);
      g.font = '10px ui-monospace,monospace';
      g.fillText(r.label || 'passthrough', x0 + 4, 11);
    }

    if (sel) {
      const x0 = Math.min(sel.a, sel.b) / dur * W, x1 = Math.max(sel.a, sel.b) / dur * W;
      g.fillStyle = 'rgba(88,166,255,.25)';
      g.fillRect(x0, 0, x1 - x0, H);
      g.fillStyle = '#58a6ff';
      g.fillRect(x0, 0, 1, H); g.fillRect(x1 - 1, 0, 1, H);
    }

    g.fillStyle = '#8b949e';
    g.font = '10px ui-monospace,monospace';
    for (let i = 0; i <= 6; i++) {
      const x = i / 6 * W;
      g.fillText(fmt(i / 6 * dur).slice(0, 5), Math.min(W - 30, x + 2), H - 3);
    }
  };

  const timeAt = ev => {
    const rect = cv.getBoundingClientRect();
    return Math.max(0, Math.min(dur, (ev.clientX - rect.left) / rect.width * dur));
  };

  cv.onmousedown = ev => { dragging = true; const t = timeAt(ev); sel = { a: t, b: t }; draw(); };
  cv.onmousemove = ev => { if (dragging) { sel.b = timeAt(ev); draw(); renderInfo(); } };
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    if (sel && Math.abs(sel.b - sel.a) < 0.5) sel = null;
    draw(); renderInfo();
  });

  function renderInfo() {
    info.innerHTML = '';
    if (!sel) {
      info.append(Object.assign(document.createElement('span'), {
        className: 'hint',
        textContent: 'Drag across the theme to select it, then listen and add it.' }));
      return;
    }
    const t0 = Math.min(sel.a, sel.b), t1 = Math.max(sel.a, sel.b);
    info.append(Object.assign(document.createElement('span'), {
      className: 'mono', textContent: `${fmt(t0)} → ${fmt(t1)} (${(t1 - t0).toFixed(1)}s)` }));
    info.append(playBtn(e.path, e.stream, t0 - 1.5, 6, '▶ at start'));
    info.append(playBtn(e.path, e.stream, t1 - 4.5, 6, '▶ at end'));
    const b = document.createElement('button');
    b.className = 'btn sm primary'; b.textContent = 'Mark as passthrough';
    b.onclick = () => {
      e.passthrough.push({ t0, t1, label: e.passthrough.length ? 'passthrough' : 'intro' });
      sel = null; saveLocal(); renderEdits();
    };
    info.append(b);
  }

  requestAnimationFrame(() => { draw(); renderInfo(); });
  window.addEventListener('resize', draw);
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
    row.append(playBtn(S.edl.edit_path, S.edl.edit_stream, s.t0, 6, '▶ start'));
    row.append(playBtn(S.edl.edit_path, S.edl.edit_stream, s.t1 - 5, 6, '▶ end'));

    const dubPath = (S.edl.dub_paths || {})[String(s.src)];
    const dubStream = (S.edl.dub_streams || {})[String(s.src)];
    if (dubPath && dubStream != null && s.src_t0 != null) {
      const off = (S.edl.dub_offsets || {})[String(s.src)] || 0;
      row.append(Object.assign(document.createElement('span'),
        { className: 'hint', style: 'margin-left:8px', textContent: 'dub' }));
      row.append(playBtn(dubPath, dubStream, s.src_t0 - off, 6, '▶ start'));
      row.append(playBtn(dubPath, dubStream,
        s.src_t0 - off + (s.t1 - s.t0) - 5, 6, '▶ end'));
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
        editDir: $('#editDir').value,
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
    if (d.editDir) $('#editDir').value = d.editDir;
    for (const e of (d.edits || [])) {
      try { e.probe = await probeOne(e.path); } catch (_) { e.probe = null; }
      e.wave = null;
      S.edits.push(e);
    }
    S.projectName = name;
    $('#projectLabel').textContent = name;
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
  if (!$('#editDir').value) $('#editDir').value = S.config.roots[0]?.path || '';

  await restoreLocal();
  if (S.projectName) $('#projectLabel').textContent = S.projectName;
  renderPairs(); renderEdits();
  await loadJobs(); await loadEdlList();
  connectEvents();
})();
