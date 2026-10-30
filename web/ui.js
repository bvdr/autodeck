// Everything that isn't audio: API calls, genre buttons, queue, library, drop zone.
const $ = (sel) => document.querySelector(sel);
// Queue-row drags carry this type so the URL drop zone can ignore them.
const ROW_TYPE = 'application/x-autodeck-id';

/** fetch JSON; server errors are always {error}, surfaced as Error(message). */
export async function api(path, method = 'GET', body) {
  const r = await fetch(path, {
    method, headers: body ? { 'content-type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `${method} ${path}: ${r.status}`);
  return j;
}

let onState = () => {};
let last = null;
let dragging = false;
let dirty = false; // a state arrived while we couldn't render; render when the owner is done
const report = (e) => { $('#msg').textContent = e.message; };

/** POST/DELETE that returns the new state, rendered immediately. */
async function act(path, method, body) {
  try { setState(await api(path, method, body)); } catch (e) { report(e); }
}

function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  // aria-* / data-* keys are attributes, not properties.
  for (const [k, v] of Object.entries(props)) k.includes('-') ? n.setAttribute(k, v) : (n[k] = v);
  n.append(...kids.filter((k) => k != null));
  return n;
}

function genreSelect(song, genres, label) {
  const s = el('select', { 'aria-label': label, title: label });
  s.append(el('option', { value: '', textContent: '(auto)' }));
  for (const g of genres) s.append(el('option', { value: g, textContent: g }));
  // effective_genre may be a model guess outside the list; show it rather than lie.
  if (song.genre && !genres.includes(song.genre)) s.append(el('option', { value: song.genre, textContent: song.genre }));
  s.value = song.genre ?? '';
  s.onchange = () => act(`/api/song/${song.id}`, 'POST', { genre: s.value || null });
  return s;
}

function badge(song) {
  return el('span', { className: `badge ${song.status}`, textContent: song.status, title: song.error || song.status });
}

function moveTo(ids, id, to) {
  const out = ids.filter((x) => x !== id);
  out.splice(Math.max(0, Math.min(out.length, to)), 0, id);
  return out;
}
const reorder = (ids, id, to) => act('/api/queue', 'POST', { ids: moveTo(ids, id, to), pin: id });
/** ↑/↓: read the queue at click time, never the (possibly stale) order the row was built from. */
function nudge(id, delta) {
  const ids = last.queue.map((q) => q.id), i = ids.indexOf(id);
  if (i >= 0) reorder(ids, id, i + delta);
}

function songRow(song, genres, queueIds) {
  const meta = [song.bpm ? `${song.bpm.toFixed(0)} BPM` : null, song.camelot,
    song.danceability != null ? `dance ${Math.round(song.danceability * 100)}%` : null].filter(Boolean).join(' · ');
  const li = el('li', { className: 'row' },
    el('span', { className: 'name' }, el('b', { textContent: song.title }), el('small', { textContent: ` ${song.channel} ${meta}` })),
    badge(song), genreSelect(song, genres, `Genre for ${song.title}`));
  if (queueIds) {
    const i = queueIds.indexOf(song.id);
    li.draggable = true;
    li.dataset.id = song.id;
    // Buttons too: drag-and-drop alone is not keyboard accessible.
    li.append(el('button', { textContent: '↑', title: 'Move up', 'aria-label': `Move ${song.title} up`, disabled: i === 0,
      onclick: () => nudge(song.id, -1) }),
    el('button', { textContent: '↓', title: 'Move down', 'aria-label': `Move ${song.title} down`,
      disabled: i === queueIds.length - 1, onclick: () => nudge(song.id, 1) }));
  }
  li.append(el('button', { className: 'remove', textContent: '✕', title: 'Remove', 'aria-label': `Remove ${song.title}`,
    onclick: () => act(`/api/song/${song.id}`, 'DELETE') }));
  return li;
}

function render(s) {
  const gb = $('#genres');
  gb.replaceChildren(...['Any', ...s.genres].map((g) => el('button', {
    textContent: g, className: 'genre', 'aria-pressed': String(g === s.genre), onclick: () => act('/api/genre', 'POST', { genre: g }),
  })));
  $('#b2b').setAttribute('aria-pressed', String(!!s.beat_to_beat));
  renderParty(s);
  renderMoods(s);
  if (s.playing !== histFor) { histFor = s.playing; if ($('#history-box').open) loadHistory(); }
  const notice = $('#notice');
  notice.hidden = !s.notice;
  notice.textContent = s.notice ?? '';
  const ids = s.queue.map((q) => q.id);
  $('#queue').replaceChildren(...s.queue.map((q) => songRow(q, s.genres, ids)));
  $('#queue-empty').hidden = s.queue.length > 0;
  $('#pool').replaceChildren(...s.pool.map((p) => songRow(p, s.genres, null)));
  const c = s.pool_counts;
  $('#pool-summary').textContent = `Library (${s.pool.length}) ` +
    Object.entries(c).map(([k, v]) => `${v} ${k}`).join(', ');
  const imp = $('#import-genre'), keep = imp.value;
  imp.replaceChildren(el('option', { value: '', textContent: 'auto-detect' }),
    ...s.genres.map((g) => el('option', { value: g, textContent: g })));
  imp.value = s.genres.includes(keep) ? keep : '';
}

/** New state from the server. Re-renders only on a version change, and never while
 * the owner is mid-drag or has a select open (a rebuilt DOM would eat the interaction). */
export function setState(s) {
  const changed = !last || s.version !== last.version || s.notice !== last.notice;
  last = s;
  const busy = dragging || document.activeElement?.tagName === 'SELECT';
  // Busy wins over dirty: once a state was deferred, every later poll (changed or not) used
  // to re-render, rebuilding the genre dropdown and closing it under the owner.
  if (busy) { if (changed) dirty = true; } else if (changed || dirty) { dirty = false; render(s); }
  onState(s);
}
export const getState = () => last;
function flush() {
  if (dirty && last && !dragging && document.activeElement?.tagName !== 'SELECT') { dirty = false; render(last); }
}

/** Pull a URL out of a drop/paste DataTransfer: uri-list first (skipping # comments). */
function urlFrom(dt) {
  const list = dt.getData('text/uri-list');
  const line = list.split(/\r?\n/).find((x) => x && !x.startsWith('#'));
  return (line || dt.getData('text/plain') || '').trim();
}

async function add(url) {
  const msg = $('#add-msg');
  if (!url) return;
  msg.textContent = 'Adding…';
  try {
    const r = await api('/api/add', 'POST', { url, genre: $('#import-genre').value || null });
    msg.textContent = `Added ${r.added} song${r.added === 1 ? '' : 's'}.`;
    $('#url').value = '';
    setState(await api('/api/state'));
  } catch (e) { msg.textContent = e.message; } // 400 {error} from the server
}

// --- moods --------------------------------------------------------------------
async function buildMood(theme) {
  const msg = $('#mood-msg');
  msg.textContent = `Reading playlists for "${theme}"…`;
  try {
    const r = await api('/api/mood', 'POST', { theme });
    msg.textContent = `${r.mood}: ${r.found} songs (${r.added} new, downloading first). Top: `
      + r.songs.slice(0, 5).map((x) => x.title).join(' · ');
    setState(await api('/api/state'));
  } catch (e) { msg.textContent = e.message; }
}

function renderMoods(s) {
  const m = s.moods ?? {};
  $('#mood-list').replaceChildren(...Object.entries(m).map(([name, n]) => el('span', { className: 'chip' },
    el('span', { textContent: `${name} (${n})` }),
    el('button', { type: 'button', textContent: '↻', title: 'Refresh: add more songs from the playlists',
      'aria-label': `Refresh mood ${name}`, onclick: () => buildMood(name) }),
    el('button', { type: 'button', className: 'remove', textContent: '✕', title: 'Delete this mood (songs stay)',
      'aria-label': `Delete mood ${name}`, onclick: () => act(`/api/mood/${encodeURIComponent(name)}`, 'DELETE') }))));
}

// --- played log ---------------------------------------------------------------
let histFor;
async function loadHistory() {
  try {
    const { history } = await api('/api/history?limit=300');
    $('#history').replaceChildren(...history.map((h) => el('li', { className: 'row' },
      el('span', { className: 'src', textContent: (h.at || '').slice(11, 16) }),
      el('span', { className: 'name' }, el('b', { textContent: h.title }),
        el('small', { textContent: ` ${h.channel}${h.bpm ? ` · ${Math.round(h.bpm)} BPM` : ''}${h.genre ? ` · ${h.genre}` : ''}` })),
      el('span', { className: 'src', textContent: h.effect ? `in via ${h.effect.replace('_', ' ')}` : 'started' }))));
    $('#history-empty').hidden = history.length > 0;
  } catch (e) { report(e); }
}

// --- party mode ---------------------------------------------------------------
const FLAVOR_LABEL = { hits: 'Hits', oldies: 'Oldies', latino: 'Latino' };
let suggFor = null; // "playing|party" the suggestions list was loaded for

function renderParty(s) {
  $('#party-btns').replaceChildren(...[null, ...(s.flavors ?? [])].map((f) => el('button', {
    textContent: f ? FLAVOR_LABEL[f] ?? f : 'Off', className: 'genre', 'aria-pressed': String((s.party ?? null) === f),
    onclick: () => act('/api/party', 'POST', { flavor: f }),
  })));
  $('#sugg-wrap').hidden = !s.party;
  const k = `${s.playing}|${s.party}`;
  if (s.party && k !== suggFor) { suggFor = k; loadSuggestions(); }
  if (!s.party) suggFor = null;
}

function suggRow(x) {
  const body = { artist: x.artist, title: x.title };
  return el('li', { className: 'row' },
    el('span', { className: 'name' }, el('b', { textContent: x.title }),
      el('small', { textContent: ` ${x.artist}${x.year ? ` · ${x.year}` : ''}` })),
    // BPM comes from Deezer (no audio yet); ✓ = beat-matchable with what's playing.
    el('span', { className: x.mixes ? 'bpm-fit' : 'src',
      textContent: x.bpm ? `${Math.round(x.bpm)} BPM${x.mixes ? ' ✓' : ''}` : '? BPM',
      title: x.mixes ? 'Beat-matches the current song' : x.bpm ? 'Tempo too far to beat-match' : 'Tempo unknown' }),
    el('span', { className: 'src', textContent: x.reason }),
    el('button', { textContent: '✓ Accept', title: 'Add it and play it soon', 'aria-label': `Accept ${x.title}`,
      onclick: async (e) => {
        e.target.disabled = true; $('#sugg-msg').textContent = `Finding ${x.title} on YouTube…`;
        try { setState(await api('/api/suggestions/accept', 'POST', body)); $('#sugg-msg').textContent = `${x.title}: preparing, plays next.`; e.target.closest('li').remove(); } catch (err) { $('#sugg-msg').textContent = err.message; e.target.disabled = false; }
      } }),
    el('button', { className: 'remove', textContent: '✕', title: 'Never suggest this again', 'aria-label': `Reject ${x.title}`,
      onclick: async () => { try { showSugg((await api('/api/suggestions/reject', 'POST', body)).suggestions); } catch (err) { report(err); } } }));
}

function showSugg(list) {
  $('#sugg').replaceChildren(...list.map(suggRow));
  $('#sugg-msg').textContent = list.length ? '' : (last?.lastfm ? 'No suggestions right now.' : 'Suggestions need a Last.fm API key (see README).');
}

async function loadSuggestions() {
  $('#sugg-msg').textContent = 'Finding suggestions…';
  try { showSugg((await api('/api/suggestions')).suggestions); } catch (e) { $('#sugg-msg').textContent = e.message; }
}

// --- search -----------------------------------------------------------------
// Case- and diacritic-insensitive, so "sarba" finds "Sârba".
const fold = (t) => (t || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
const mmss = (s) => (s ? `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}` : '');

function resultRow(r, source) {
  const li = el('li', { className: 'row' },
    el('span', { className: 'name' }, el('b', { textContent: r.title }),
      el('small', { textContent: ` ${r.channel || ''} ${mmss(r.duration)}` })),
    el('span', { className: 'src', textContent: source }),
    r.status ? badge(r) : null,
    el('button', { textContent: 'Play next', title: 'Prepare now and play it next', 'aria-label': `Play ${r.title} next`,
      onclick: () => act('/api/request', 'POST', { id: r.id, title: r.title, channel: r.channel || '', duration: r.duration || 0 }) }));
  return li;
}

async function search(q) {
  const msg = $('#search-msg'), out = $('#results');
  q = q.trim();
  if (!q) { out.replaceChildren(); msg.textContent = ''; return; }
  const words = fold(q).split(/\s+/);
  const local = (last?.pool ?? []).filter((s) => words.every((w) => fold(`${s.title} ${s.channel}`).includes(w))).slice(0, 10);
  out.replaceChildren(...local.map((s) => resultRow(s, 'library')));
  msg.textContent = 'Searching YouTube…';
  try {
    const { results } = await api(`/api/search?${new URLSearchParams({ q })}`);
    const have = new Set(local.map((s) => s.id));
    out.append(...results.filter((r) => !have.has(r.id)).map((r) => resultRow(r, 'YouTube')));
    msg.textContent = out.children.length ? '' : 'No results.';
  } catch (e) { msg.textContent = e.message; }
}

export function initUi(stateCb) {
  onState = stateCb;
  $('#sugg-refresh').addEventListener('click', loadSuggestions);
  $('#mood-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const t = $('#mood-theme').value.trim();
    if (t) { buildMood(t); $('#mood-theme').value = ''; }
  });
  $('#history-box').addEventListener('toggle', (e) => { if (e.target.open) loadHistory(); });
  $('#search-form').addEventListener('submit', (e) => { e.preventDefault(); search($('#q').value); });
  // Queue drag & drop -> POST /api/queue with pin = the dragged song.
  const q = $('#queue');
  q.addEventListener('dragstart', (e) => {
    const li = e.target.closest('li[data-id]');
    if (!li) return;
    dragging = true;
    e.dataTransfer.setData(ROW_TYPE, li.dataset.id);
    e.dataTransfer.effectAllowed = 'move';
  });
  q.addEventListener('dragend', () => { dragging = false; flush(); });
  // A select closing (or losing focus) releases a deferred render.
  document.addEventListener('focusout', (e) => { if (e.target.tagName === 'SELECT') setTimeout(flush); });
  q.addEventListener('dragover', (e) => { if (e.dataTransfer.types.includes(ROW_TYPE)) e.preventDefault(); });
  q.addEventListener('drop', (e) => {
    const id = e.dataTransfer.getData(ROW_TYPE);
    if (!id || !last) return;
    e.preventDefault();
    dragging = false;
    const ids = last.queue.map((x) => x.id);
    const target = e.target.closest('li[data-id]');
    // Dropped on a row: take that row's slot (moveTo removes the dragged id first, so
    // dragging down lands after the target and dragging up lands before it).
    reorder(ids, id, target ? ids.indexOf(target.dataset.id) : ids.length);
  });

  const zone = $('#drop');
  const isUrlDrag = (e) => !e.dataTransfer.types.includes(ROW_TYPE)
    && (e.dataTransfer.types.includes('text/uri-list') || e.dataTransfer.types.includes('text/plain'));
  zone.addEventListener('dragover', (e) => { if (isUrlDrag(e)) { e.preventDefault(); zone.classList.add('over'); } });
  zone.addEventListener('dragleave', () => zone.classList.remove('over'));
  zone.addEventListener('drop', (e) => {
    zone.classList.remove('over');
    if (!isUrlDrag(e)) return;
    e.preventDefault();
    add(urlFrom(e.dataTransfer));
  });
  // Pasting onto the focused zone adds directly; pasting into the URL box just fills it.
  zone.addEventListener('paste', (e) => {
    if (e.target.id === 'url') return;
    e.preventDefault();
    add(urlFrom(e.clipboardData));
  });
  $('#add-form').addEventListener('submit', (e) => { e.preventDefault(); add($('#url').value.trim()); });
}
