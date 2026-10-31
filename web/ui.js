// Everything that isn't audio: API calls, the vibe panel (genres, moods, party), Up next,
// Played / Pairs / Library, search, "Or start with", add-by-link, notices and phone tabs.
import { sortable } from './sortable.js';
const $ = (sel) => document.querySelector(sel);
// Queue-row drags carry this type so the URL drop zone can ignore them.
const ROW_TYPE = 'application/x-autodeck-id';

export const FX_LABEL = { blend: 'Beat blend', half_double: 'Beat blend (½×/2×)', bass_swap: 'Bass swap',
  filter_sweep: 'Filter sweep', echo: 'Echo out', loop_roll: 'Loop roll', brake: 'Brake', backspin: 'Backspin',
  reverb: 'Reverb wash', gate: 'Gate / stutter', riser: 'Riser → drop', dropswap: 'Drop swap', cut: 'Cut', fade: 'Fade' };
// Lucide icon per effect (the design's effect picker and Played rows).
export const FX_ICON = { blend: 'blend', half_double: 'blend', bass_swap: 'arrow-left-right', filter_sweep: 'audio-waveform',
  echo: 'radio', loop_roll: 'repeat', brake: 'disc-3', backspin: 'rotate-ccw', reverb: 'waves', gate: 'audio-lines',
  riser: 'trending-up', dropswap: 'arrow-down-to-line', cut: 'scissors', fade: 'audio-lines' };

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
let hooks = { loadOpener: () => {}, isLive: () => false };
let last = null;
let dragging = false;
let dirty = false; // a state arrived while we couldn't render; render when the owner is done
let msgTimer = 0;
/** Owner-facing error: the red banner (role=alert), gone after 10 s. */
export function report(e) {
  const m = $('#msg');
  m.querySelector('span').textContent = e.message ?? String(e);
  m.hidden = false;
  clearTimeout(msgTimer);
  msgTimer = setTimeout(() => { m.hidden = true; }, 10000);
}

/** POST/DELETE that returns the new state, rendered immediately. */
async function act(path, method, body) {
  try { setState(await api(path, method, body)); } catch (e) { report(e); }
}

const SVG = 'http://www.w3.org/2000/svg';
/** <svg class="i"><use href="#i-name"/></svg> from the inline Lucide sprite. */
export function icon(name, cls = '') {
  const s = document.createElementNS(SVG, 'svg'), u = document.createElementNS(SVG, 'use');
  s.setAttribute('class', `i ${cls}`.trim());
  s.setAttribute('aria-hidden', 'true');
  u.setAttribute('href', `#i-${name}`);
  s.append(u);
  return s;
}

function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  // aria-* / data-* keys are attributes, not properties.
  for (const [k, v] of Object.entries(props)) k.includes('-') ? n.setAttribute(k, v) : (n[k] = v);
  n.append(...kids.flat().filter((k) => k != null && k !== false));
  return n;
}
const iconBtn = (name, label, onclick, cls = '') => el('button', { type: 'button', className: `icon-btn ${cls}`.trim(),
  title: label, 'aria-label': label, onclick }, icon(name));

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

const STATUS_ICON = { ready: 'circle-check', analyzing: 'loader', downloading: 'loader', queued: 'hourglass', failed: 'circle-alert' };
const cap = (t) => t[0].toUpperCase() + t.slice(1);
/** Status badge: icon + word, so status never rests on colour alone. */
function badge(kind, text = cap(kind), title = '') {
  const spin = kind === 'analyzing' || kind === 'downloading';
  return el('span', { className: `badge ${kind}`, title: title || text },
    icon(STATUS_ICON[kind] ?? { pinned: 'pin', locked: 'lock', loved: 'flame', avoided: 'thumbs-down', mixes: 'check' }[kind] ?? 'info', spin ? 'spin' : ''), text);
}
const songMeta = (s) => [s.bpm ? s.bpm.toFixed(1) : '–', s.camelot ?? '–',
  s.danceability != null ? `${Math.round(s.danceability * 100)}%` : '–'].join('  ');

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
const titleOf = (id) => last?.pool.find((p) => p.id === id)?.title ?? id;

// --- Up next --------------------------------------------------------------------
function queueRow(song, i, s, ids, prevId) {
  const locked = i < (s.queue_locked ?? 0);
  const pinned = (s.pinned ?? []).includes(song.id);
  const pair = prevId ? s.pairs?.[`${prevId}>${song.id}`] : null;
  const loved = pair === 'loved';
  const li = el('li', { className: `row${locked ? ' locked' : ''}${loved ? ' loved' : ''}` },
    // Locked rows (already planned) have a lock instead of a grip: sortable.js only lifts grips.
    locked ? el('span', { className: 'lead', title: 'Locked in: already planned' }, icon('lock'), el('span', { className: 'sr-only', textContent: 'Locked in.' }))
      : el('span', { className: 'lead grip', title: 'Drag to reorder', 'aria-hidden': 'true' }, icon('grip-vertical')),
    el('span', { className: 'text' },
      el('span', { className: 't' }, song.title, loved ? ' ' : null, loved ? icon('flame') : null),
      loved ? el('span', { className: 's why', textContent: `You loved it after ${titleOf(prevId)}` })
        : el('span', { className: 's', textContent: song.channel })),
    el('span', { className: 'meta', textContent: songMeta(song), title: 'BPM  key  danceability' }),
    loved ? badge('loved', 'Loved after this') : pair === 'avoided' ? badge('avoided', 'Avoided pair')
      : locked ? badge('locked', 'Locked in') : pinned ? badge('pinned') : badge(song.status, cap(song.status), song.error),
    genreSelect(song, s.genres, `Genre for ${song.title}`),
    el('span', { className: 'acts' },
      // Buttons too: drag-and-drop alone is not keyboard accessible.
      locked ? null : iconBtn('arrow-up', `Move ${song.title} up`, () => nudge(song.id, -1)),
      locked ? null : iconBtn('arrow-down', `Move ${song.title} down`, () => nudge(song.id, 1)),
      iconBtn('x', `Remove ${song.title}`, () => act(`/api/song/${song.id}`, 'DELETE'), 'remove')));
  li.dataset.id = song.id;
  const [up, down] = li.querySelectorAll('.acts .icon-btn');
  if (!locked) { up.disabled = i <= (s.queue_locked ?? 0); down.disabled = i === ids.length - 1; }
  return li;
}

function renderQueue(s) {
  const ids = s.queue.map((q) => q.id);
  $('#queue').replaceChildren(...s.queue.map((q, i) => queueRow(q, i, s, ids, i ? s.queue[i - 1].id : s.playing)));
  // "Play next" picks still downloading / analysing: shown first, not draggable yet.
  $('#requested').replaceChildren(...(s.requested ?? []).map((q) => el('li', { className: 'row' },
    el('span', { className: 'lead' }, icon('list-start')),
    el('span', { className: 'text' }, el('span', { className: 't', textContent: q.title }),
      el('span', { className: q.status === 'failed' ? 's err' : 's', textContent: q.error || 'Plays next once ready' })),
    badge(q.status, cap(q.status), q.error),
    el('span', { className: 'acts' }, iconBtn('x', `Remove ${q.title}`, () => act(`/api/song/${q.id}`, 'DELETE'), 'remove')))));
  $('#queue-empty').hidden = s.queue.length + (s.requested?.length ?? 0) > 0;
  const c = s.pool_counts, working = (c.analyzing ?? 0) + (c.downloading ?? 0);
  const what = s.party ? `${cap(s.party)} party` : s.genre;
  $('#queue-count').textContent = [what, `${c.ready ?? 0} ready`, working ? `${working} analyzing` : null,
    c.queued ? `${c.queued} queued` : null].filter(Boolean).join(' · ');
  const total = Math.max(1, s.pool.length);
  $('#queue-progress .r').style.width = `${((c.ready ?? 0) / total) * 100}%`;
  $('#queue-progress .w').style.width = `${(working / total) * 100}%`;
}

// --- render -------------------------------------------------------------------------
function render(s) {
  const moods = s.moods ?? {};
  renderGenres(s, moods);
  $('#b2b').setAttribute('aria-checked', String(!!s.beat_to_beat));
  renderParty(s);
  renderMoods(s, moods);
  const notice = $('#notice');
  notice.hidden = !s.notice;
  notice.querySelector('span').textContent = s.notice ?? '';
  renderQueue(s);
  renderLogs(s);
  renderOpeners(s);
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

// --- genres + moods ----------------------------------------------------------------
function renderGenres(s, moods) {
  const box = $('#genres');
  const plain = s.genres.filter((g) => !(g in moods));
  box.replaceChildren(...['Any', ...plain].map((g) => el('button', {
    type: 'button', textContent: g, className: 'chip', 'aria-pressed': String(g === s.genre && !s.party),
    onclick: () => act('/api/genre', 'POST', { genre: g }),
  })), el('button', { type: 'button', className: 'chip ghost build', hidden: !$('#mood-form').hidden,
    onclick: openComposer }, icon('plus'), 'Build a mood'));
}

// Mood composer (design flow HGBmU): opens in place of the "+ Build a mood" chip, checks the
// name as you type ("Any" is reserved, an existing name means Refresh), closes while the
// playlists are read (a progress chip stands in), reopens with the text on an error.
const building = new Map(); // mood name -> true while its playlists are being read
const moodName = (t) => { t = t.split(/\s+/).filter(Boolean).join(' ').slice(0, 60); return t && t[0].toUpperCase() + t.slice(1); };

function openComposer(text = '') {
  $('#mood-form').hidden = false;
  $('#genres .build')?.setAttribute('hidden', '');
  $('#mood-theme').value = typeof text === 'string' ? text : '';
  checkMood();
  $('#mood-theme').focus();
}
function closeComposer() {
  $('#mood-form').hidden = true;
  $('#genres .build')?.removeAttribute('hidden');
  $('#mood-theme').closest('.field').classList.remove('bad');
  $('#mood-err').textContent = '';
}
/** Live validation; returns the tidied name or null if it can't be sent. */
function checkMood() {
  const name = moodName($('#mood-theme').value), err = $('#mood-err'), btn = $('#mood-build');
  const exists = name && Object.keys(last?.moods ?? {}).find((m) => m.toLowerCase() === name.toLowerCase());
  let bad = '', warn = '';
  if (name.toLowerCase() === 'any') bad = '"Any" is the all-genres filter. Pick another name.';
  else if (exists) warn = `${exists} already exists. Refresh adds new picks to it; nothing is removed.`;
  err.className = `field-msg ${bad ? 'bad' : warn ? 'warn' : ''}`;
  err.replaceChildren(...(bad || warn ? [icon('circle-alert'), bad || warn] : []));
  $('#mood-theme').closest('.field').classList.toggle('bad', !!bad);
  btn.disabled = !name || !!bad;
  btn.replaceChildren(icon(exists ? 'refresh-cw' : 'sparkles'), el('span', { textContent: exists ? 'Refresh' : 'Build' }));
  return name && !bad ? (exists || name) : null;
}

function moodReport(kind, text) {
  const m = $('#mood-msg');
  m.className = `report ${kind}`;
  m.replaceChildren(icon(kind === 'ok' ? 'circle-check' : kind === 'failed' ? 'circle-alert' : 'info'), el('span', { textContent: text }));
  m.hidden = false;
}

async function buildMood(theme, typed = theme) {
  const name = moodName(theme), before = last?.moods?.[name];
  building.set(name, true);
  closeComposer();
  renderMoods(last, last.moods ?? {});
  $('#mood-msg').hidden = true;
  try {
    const r = await api('/api/mood', 'POST', { theme });
    const s = await api('/api/state');
    const n = s.moods?.[r.mood] ?? r.found;
    building.delete(name);
    moodReport('ok', before != null
      ? `${r.mood}: ${n - before} new picks added, ${n} songs now. Nothing was removed.`
      : `${r.mood} is on: ${r.found} songs, ${r.added} new (downloading first). Top: ${r.songs.slice(0, 3).map((x) => x.title).join(' · ')}`);
    setState(s);
  } catch (e) {
    building.delete(name);
    renderMoods(last, last.moods ?? {});
    // Errors stay in the composer, next to what was typed, so fixing one is a single edit.
    openComposer(typed); // exactly what the owner typed, so fixing it is one edit
    const err = $('#mood-err');
    err.className = 'field-msg bad';
    err.replaceChildren(icon('circle-alert'), e.message.startsWith('No playlists')
      ? `No public playlists found for "${theme}". Try something broader.` : e.message);
    $('#mood-theme').closest('.field').classList.add('bad');
  }
}

function renderMoods(s, m) {
  const chips = Object.entries(m).filter(([name]) => !building.has(name)).map(([name, n]) => {
    const on = s.genre === name && !s.party;
    return el('span', { className: `mood-chip${on ? ' on' : ''}` },
      el('button', { type: 'button', className: 'name', 'aria-pressed': String(on),
        onclick: () => act('/api/genre', 'POST', { genre: name }) }, name, el('span', { className: 'n', textContent: String(n) })),
      iconBtn('refresh-cw', `Refresh mood ${name}: add more songs from the playlists`, () => buildMood(name)),
      iconBtn('x', `Delete mood ${name} (songs stay)`, async () => {
        const wasOn = s.genre === name;
        await act(`/api/mood/${encodeURIComponent(name)}`, 'DELETE');
        moodReport('info', `Removed ${name}. Its ${n} songs stay in your library.${wasOn ? ' Back to Any.' : ''}`);
      }));
  });
  const progress = [...building.keys()].map((name) => el('span', { className: 'mood-chip building', role: 'status' },
    icon('loader', 'spin'), name, el('small', { textContent: 'reading playlists…' })));
  $('#mood-list').replaceChildren(...progress, ...chips);
}

// --- played / pairs / library ----------------------------------------------------------
let logTab = 'played', histFor, pairsFor, pairFilter = 'all', libFilter = 'all';

function renderLogs(s) {
  $('#tab-pairs .n').textContent = String(Object.keys(s.pairs ?? {}).length);
  $('#tab-library .n').textContent = String(s.pool.length);
  if (logTab === 'played' && s.playing !== histFor) { histFor = s.playing; loadHistory(); }
  const pk = JSON.stringify(s.pairs ?? {});
  if (logTab === 'pairs' && pk !== pairsFor) { pairsFor = pk; loadPairs(); }
  if (logTab === 'library') renderLibrary(s);
}

function showTab(name) {
  logTab = name;
  for (const t of ['played', 'pairs', 'library']) {
    const on = t === name;
    $(`#tab-${t}`).setAttribute('aria-selected', String(on));
    $(`#tab-${t}`).tabIndex = on ? 0 : -1;
    $(`#pane-${t}`).hidden = !on;
  }
  histFor = pairsFor = undefined; // reload what the tab shows
  if (last) renderLogs(last);
}

const FLAME = 'flame', THUMB = 'thumbs-down';
async function ratePair(from, to, verdict) {
  try { setState(await api('/api/pair', 'POST', { from, to, verdict })); loadHistory(); } catch (e) { report(e); }
}

async function loadHistory() {
  try {
    const { history } = await api('/api/history?limit=300');
    $('#history').replaceChildren(...history.map((h) => {
      const v = h.verdict;
      const rateBtn = (kind, ic, label) => {
        const b = iconBtn(ic, label, () => ratePair(h.from, h.id, v === kind ? null : kind), `rate ${kind === 'loved' ? 'love' : 'avoid'}`);
        b.setAttribute('aria-pressed', String(v === kind));
        return b;
      };
      return el('li', { className: 'logrow' },
        el('span', { className: 'at', textContent: (h.at || '').slice(11, 16) }),
        el('span', { className: 't', textContent: h.title }),
        el('span', { className: 'meta', textContent: [h.bpm ? h.bpm.toFixed(1) : null, h.genre].filter(Boolean).join(' · ') }),
        el('span', { className: 'fx' }, h.effect ? [icon(FX_ICON[h.effect] ?? 'blend'), FX_LABEL[h.effect] ?? h.effect] : 'Started'),
        // A row rates the hand-over INTO this song; a song started from scratch has none.
        h.from ? rateBtn('loved', FLAME, `Nailed it: the mix from ${titleOf(h.from)} into ${h.title}`) : el('span', { className: 'rate' }),
        h.from ? rateBtn('avoided', THUMB, `Didn't work: the mix from ${titleOf(h.from)} into ${h.title}`) : el('span', { className: 'rate' }));
    }));
    $('#history-empty').hidden = history.length > 0;
  } catch (e) { report(e); }
}

async function loadPairs() {
  try {
    const { pairs } = await api('/api/pairs');
    const n = { all: pairs.length, loved: pairs.filter((p) => p.verdict === 'loved').length };
    n.avoided = n.all - n.loved;
    $('#pair-filter').replaceChildren(...['all', 'loved', 'avoided'].map((k) => el('button', { type: 'button', className: 'chip',
      'aria-pressed': String(pairFilter === k), onclick: () => { pairFilter = k; pairsFor = undefined; renderLogs(last); } },
    `${cap(k)} ${n[k]}`)));
    const shown = pairs.filter((p) => pairFilter === 'all' || p.verdict === pairFilter);
    $('#pairs').replaceChildren(...shown.map((p) => el('li', { className: 'logrow' },
      el('span', { className: 'text' },
        el('span', { className: 't pair-t' }, p.from_title, icon('arrow-right'), p.to_title),
        el('span', { className: 's', textContent: p.verdict === 'loved' ? 'You loved this hand-over' : "Songs don't go together" })),
      p.verdict === 'loved' ? badge('loved', 'Loved') : badge('avoided', 'Avoid'),
      // Play pair: both songs next, in this order (the owner's "play next" list keeps order).
      p.verdict === 'loved' ? el('button', { type: 'button', className: 'btn', onclick: async () => {
        await act('/api/request', 'POST', { id: p.from, title: p.from_title });
        await act('/api/request', 'POST', { id: p.to, title: p.to_title });
      } }, icon('list-start'), 'Play pair') : null,
      iconBtn('x', `Forget ${p.from_title} → ${p.to_title}`, () => ratePair(p.from, p.to, null), 'remove'))));
    $('#pairs-empty').hidden = pairs.length > 0;
  } catch (e) { report(e); }
}

function renderLibrary(s) {
  const working = (p) => p.status === 'downloading' || p.status === 'analyzing';
  const n = { all: s.pool.length, failed: s.pool.filter((p) => p.status === 'failed').length, working: s.pool.filter(working).length };
  $('#lib-filter').replaceChildren(...['all', 'failed', 'working'].map((k) => el('button', { type: 'button', className: 'chip',
    'aria-pressed': String(libFilter === k), onclick: () => { libFilter = k; renderLibrary(last); } }, `${cap(k)} ${n[k]}`)));
  const rows = s.pool.filter((p) => libFilter === 'all' || (libFilter === 'failed' ? p.status === 'failed' : working(p)));
  rows.sort((a, b) => (b.status === 'failed') - (a.status === 'failed')); // failed first: they need the owner
  $('#pool').replaceChildren(...rows.map((p) => el('li', { className: 'logrow' },
    el('span', { className: 'text' }, el('span', { className: 't', textContent: p.title }),
      el('span', { className: p.status === 'failed' ? 's err' : 's', textContent: p.status === 'failed' ? p.error || 'Failed' : p.channel })),
    badge(p.status, cap(p.status), p.error),
    p.status === 'failed' ? el('button', { type: 'button', className: 'btn', onclick: () => act(`/api/song/${p.id}/retry`, 'POST') }, icon('rotate-ccw'), 'Retry')
      : genreSelect(p, s.genres, `Genre for ${p.title}`),
    iconBtn('x', `Remove ${p.title}`, () => act(`/api/song/${p.id}`, 'DELETE'), 'remove'))));
}

// --- party mode ---------------------------------------------------------------
const FLAVOR_LABEL = { hits: 'Hits', oldies: 'Oldies', latino: 'Latino' };
let suggFor = null; // "playing|party" the suggestions list was loaded for

function renderParty(s) {
  $('#party-btns').replaceChildren(...[null, ...(s.flavors ?? [])].map((f) => el('button', {
    type: 'button', textContent: f ? FLAVOR_LABEL[f] ?? f : 'Off', 'aria-pressed': String((s.party ?? null) === f),
    onclick: () => act('/api/party', 'POST', { flavor: f }),
  })));
  // Desktop has room for a short caption; the phone's Vibe tab explains a bit more (design).
  $('#party-hint').replaceChildren(
    el('span', { className: 'desk-only', textContent: s.party ? 'Crowd-pleasers first, energy building' : 'Crowd-pleasers first' }),
    el('span', { className: 'phone-only', textContent: s.party
      ? 'Crowd-pleasers first, energy building. Genres and moods are paused while Party is on.'
      : 'Off: your genre or mood picks the songs. Hits, Oldies and Latino play crowd-pleasers and pause genres and moods.' }));
  $('#sugg-wrap').hidden = !s.party;
  const k = `${s.playing}|${s.party}`;
  if (s.party && k !== suggFor) { suggFor = k; loadSuggestions(); }
  if (!s.party) suggFor = null;
}

function suggRow(x) {
  const body = { artist: x.artist, title: x.title };
  const accept = el('button', { type: 'button', className: 'btn primary', 'aria-label': `Accept ${x.title}`,
    title: 'Add it and play it soon', onclick: async () => {
      // In place: the button becomes a working state while YouTube is searched.
      accept.replaceWith(working);
      try {
        setState(await api('/api/suggestions/accept', 'POST', body));
        $('#sugg-msg').textContent = `${x.title}: preparing, plays next.`;
        working.closest('li')?.remove();
      } catch (err) { working.replaceWith(accept); $('#sugg-msg').textContent = err.message; }
    } }, icon('check'), 'Accept');
  const working = el('span', { className: 'badge working' }, icon('loader', 'spin'), 'Finding on YouTube…');
  return el('li', { className: 'row sugg-row' },
    el('span', { className: 'text' }, el('span', { className: 't', textContent: x.title }),
      el('span', { className: 's', textContent: [x.artist, x.year, x.reason].filter(Boolean).join(' · ') })),
    // BPM comes from Deezer (no audio yet); "mixes" = beat-matchable with what's playing.
    el('span', { className: 'meta', textContent: x.bpm ? `${x.bpm.toFixed(1)} BPM` : '? BPM',
      title: x.mixes ? 'Beat-matches the current song' : x.bpm ? 'Tempo too far to beat-match' : 'Tempo unknown' }),
    x.mixes ? badge('mixes', 'mixes') : null,
    el('span', { className: 'acts' },
      iconBtn('x', `Never suggest ${x.title} again`, async () => {
        try { showSugg((await api('/api/suggestions/reject', 'POST', body)).suggestions); } catch (err) { report(err); }
      }, 'remove'), accept));
}

function showSugg(list) {
  $('#sugg').replaceChildren(...list.map(suggRow));
  if (list.length) { $('#sugg-msg').textContent = ''; return; }
  if (last?.lastfm) { $('#sugg-msg').textContent = 'No suggestions right now.'; return; }
  // No Last.fm key: a setup hint instead of an empty list (party ordering still works).
  $('#sugg-msg').replaceChildren(el('span', { className: 'key-hint' },
    el('b', {}, icon('key-round'), ' Suggestions need a free Last.fm API key'),
    el('button', { type: 'button', className: 'btn', onclick: () => togglePop('settings', true) }, icon('settings'), 'Add it in Settings'),
    el('a', { href: 'https://www.last.fm/api/account/create', target: '_blank', rel: 'noopener' }, 'Get a key at last.fm/api')));
}

async function loadSuggestions() {
  $('#sugg-msg').textContent = 'Finding suggestions…';
  try { showSugg((await api('/api/suggestions')).suggestions); } catch (e) { $('#sugg-msg').textContent = e.message; }
}

// --- search -----------------------------------------------------------------
// Case- and diacritic-insensitive, so "sarba" finds "Sârba".
const fold = (t) => (t || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase();
const mmss = (s) => (s ? `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}` : '');
const localMatches = (q, n = 10) => {
  const words = fold(q).split(/\s+/).filter(Boolean);
  return (last?.pool ?? []).filter((s) => words.every((w) => fold(`${s.title} ${s.channel}`).includes(w))).slice(0, n);
};

function resultRow(r, inLibrary) {
  const li = el('li', { className: 'row res-row' },
    el('span', { className: 'text' }, el('span', { className: 't', textContent: r.title }),
      el('span', { className: 's' }, r.status ? badge(r.status) : null,
        [r.channel, mmss(r.duration)].filter(Boolean).join(' · '))),
    el('button', { type: 'button', className: 'btn', title: 'Prepare now and play it next', 'aria-label': `Play ${r.title} next`,
      onclick: async () => {
        await act('/api/request', 'POST', { id: r.id, title: r.title, channel: r.channel || '', duration: r.duration || 0 });
        $('#search-msg').textContent = r.status === 'ready' ? `${r.title} plays next.` : `Preparing "${r.title}"… it plays next once ready.`;
      } }, icon('list-start'), el('span', { textContent: 'Play next' })));
  return li;
}

function showLocal(q) {
  const local = localMatches(q);
  $('#res-lib').replaceChildren(...local.map((s) => resultRow(s, true)));
  $('#res-lib-h').hidden = !q;
  $('#res-lib-h').textContent = `In your library ${local.length} · instant`;
  return local;
}

async function search(q) {
  const msg = $('#search-msg');
  q = q.trim();
  if (!q) { $('#res-lib').replaceChildren(); $('#res-yt').replaceChildren(); $('#res-yt-h').hidden = $('#res-lib-h').hidden = true; msg.textContent = ''; return; }
  const local = showLocal(q);
  msg.textContent = 'Searching YouTube…';
  try {
    const { results } = await api(`/api/search?${new URLSearchParams({ q })}`);
    const have = new Set(local.map((s) => s.id));
    const yt = results.filter((r) => !have.has(r.id));
    $('#res-yt').replaceChildren(...yt.map((r) => resultRow(r, false)));
    $('#res-yt-h').hidden = false;
    $('#res-yt-h').textContent = `YouTube ${yt.length}`;
    msg.textContent = local.length + yt.length ? '' : 'No results.';
  } catch (e) { msg.textContent = e.message; }
}

// --- "Or start with" (before the first song) ------------------------------------
let opTab = 'good', openerId = null;
export function setOpener(id) { openerId = id; if (last) renderOpeners(last); }

function renderOpeners(s) {
  if (hooks.isLive()) return;
  const ready = s.pool.filter((p) => p.status === 'ready');
  const q = $('#op-find').value.trim();
  const lovedFrom = new Set(Object.entries(s.pairs ?? {}).filter(([, v]) => v === 'loved').map(([k]) => k.split('>')[0]));
  const list = q ? localMatches(q, 12).filter((p) => p.status === 'ready')
    : opTab === 'loved' ? ready.filter((p) => lovedFrom.has(p.id))
      : opTab === 'recent' ? [...ready].reverse().slice(0, 12)
        : s.queue.slice(0, 12);
  const why = (p) => (opTab === 'loved' && !q ? 'You loved what came after it' : [p.genre, p.bpm ? `${p.bpm.toFixed(1)} BPM` : null,
    p.camelot, p.danceability != null ? `${Math.round(p.danceability * 100)}% danceable` : null].filter(Boolean).join(' · '));
  $('#op-list').replaceChildren(...list.map((p) => el('li', { className: 'row op-row' },
    el('span', { className: 'text' }, el('span', { className: 't', textContent: p.title }), el('span', { className: 's', textContent: why(p) })),
    p.id === openerId ? el('span', { className: 'on-play' }, icon('play'), 'On PLAY')
      : el('button', { type: 'button', className: 'btn', 'aria-label': `Load ${p.title} in A`, onclick: () => hooks.loadOpener(p) },
        icon('arrow-down-to-line'), 'Load in A'))));
  if (!list.length) $('#op-list').append(el('li', { className: 'empty-note', textContent: q ? 'No ready song matches.' : 'Nothing here yet.' }));
  $('#op-foot span').textContent = `Up next is planned from whatever plays first: ${s.queue.length} ready in ${s.party ? `${cap(s.party)} party` : s.genre}.`;
  for (const b of document.querySelectorAll('[data-op]')) b.setAttribute('aria-pressed', String(b.dataset.op === opTab));
}

// --- add by link ----------------------------------------------------------------
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

// Header popovers: one open at a time. `btns` carry aria-expanded, `focus` gets focus on open.
const POPS = {
  add: { pop: '#add-pop', btns: ['#add-btn'], focus: '#url' },
  settings: { pop: '#settings-pop', btns: ['#settings-btn'], focus: '#lastfm-key', onOpen: () => loadSettings() },
  menu: { pop: '#menu-pop', btns: ['#menu-btn'], focus: '#menu-add' },
};
function togglePop(name, open) {
  const p = POPS[name];
  open ??= $(p.pop).hidden;
  if (open) for (const k of Object.keys(POPS)) if (k !== name) togglePop(k, false);
  $(p.pop).hidden = !open;
  for (const b of p.btns) $(b).setAttribute('aria-expanded', String(open));
  if (open) { p.onOpen?.(); $(p.focus).focus(); }
}
const closePops = () => { for (const k of Object.keys(POPS)) togglePop(k, false); };

// --- settings: Last.fm key -------------------------------------------------------------
/** Instant format check, same rule as the server (which also asks Last.fm). null = looks fine. */
export function keyProblem(key) {
  const k = (key ?? '').trim();
  if (!k) return 'Paste a key first';
  return /^[0-9a-fA-F]{32}$/.test(k) ? null : 'A Last.fm API key is 32 letters and digits (0-9, a-f)';
}
/** What Settings says about the key, from GET /api/settings' `lastfm` (never the key itself). */
export function lastfmState(lf) {
  if (!lf?.set) return { text: 'Not set — party suggestions and Hits/Oldies/Latino need it', cls: 'unset', removable: false };
  const from = lf.source === 'env' ? ' (from the LASTFM_API_KEY environment variable)'
    : lf.source === 'legacy' ? ' (from ~/.config/autodeck/lastfm.env; saving one here takes over)' : '';
  // Remove deletes only the key saved from Settings: an env var or the old config file isn't
  // the page's to delete, and "removing" it would silently do nothing.
  return { text: `Set · ends in …${lf.hint}${from}`, cls: 'set', removable: lf.source === 'settings' };
}
function showSettings(st) {
  const k = lastfmState(st.lastfm);
  const el = $('#lastfm-state');
  el.className = `key-state ${k.cls}`;
  el.replaceChildren(icon(k.cls === 'set' ? 'circle-check' : 'info'), k.text);
  $('#lastfm-remove').hidden = !k.removable;
  if (st.library) $('#lib-path').textContent = st.library;
}
async function loadSettings() {
  try { showSettings(await api('/api/settings')); } catch (e) { $('#lastfm-err').textContent = e.message; }
}
async function saveKey(key) {
  const err = $('#lastfm-err');
  const bad = key ? keyProblem(key) : null;
  if (bad) { err.textContent = bad; return; }
  err.textContent = '';
  $('#lastfm-state').textContent = key ? 'Checking with Last.fm…' : 'Removing…';
  try {
    showSettings(await api('/api/settings/lastfm', 'PUT', { key }));
    $('#lastfm-key').value = ''; // never keep the key in the page once saved
    suggFor = null; // suggestions may work now (or stop): reload them on the next state
    setState(await api('/api/state'));
  } catch (e) {
    err.textContent = e.message; // the server's 400 {error}: bad format or Last.fm said no
    loadSettings();
  }
}

// --- phone tabs --------------------------------------------------------------------
export function toTab(name) {
  document.body.dataset.tab = name;
  for (const b of document.querySelectorAll('.tabbar button')) {
    if (b.dataset.tab === name) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  }
  if (matchMedia('(max-width: 600px)').matches) window.scrollTo(0, 0);
}

export function initUi(stateCb, h = {}) {
  onState = stateCb;
  hooks = { ...hooks, ...h };
  $('#sugg-refresh').addEventListener('click', loadSuggestions);
  // Mood composer
  $('#mood-theme').addEventListener('input', checkMood);
  $('#mood-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const t = checkMood();
    if (t) buildMood(t, $('#mood-theme').value);
  });
  $('#mood-form').addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeComposer(); $('#genres .build')?.focus(); } });
  $('#mood-close').addEventListener('click', () => { closeComposer(); $('#genres .build')?.focus(); });
  for (const b of document.querySelectorAll('.ideas .chip')) b.addEventListener('click', () => { $('#mood-theme').value = b.textContent; checkMood(); $('#mood-theme').focus(); });
  // Played / Pairs / Library tabs (arrow keys move between them, per the ARIA tabs pattern).
  const tabs = ['played', 'pairs', 'library'];
  for (const t of tabs) $(`#tab-${t}`).addEventListener('click', () => showTab(t));
  $('.tabs').addEventListener('keydown', (e) => {
    const d = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (!d) return;
    const next = tabs[(tabs.indexOf(logTab) + d + tabs.length) % tabs.length];
    showTab(next);
    $(`#tab-${next}`).focus();
  });
  // Search: library matches as you type, YouTube on Enter. Desktop: a popover; phone: the tab.
  const q = $('#q'), panel = $('#search-panel');
  // Desktop: the panel hangs under the header field. Placed from the field's box once per
  // open (one layout read, not per frame); the phone's CSS makes it static in the Search tab.
  const place = () => {
    const r = $('#search-form').getBoundingClientRect();
    Object.assign(panel.style, { top: `${r.bottom + window.scrollY + 8}px`, left: `${r.left + window.scrollX}px` });
  };
  const open = (on) => { if (on && panel.hidden) place(); panel.hidden = !on; };
  q.addEventListener('input', () => { const v = q.value.trim(); open(!!v); showLocal(v); if (!v) search(''); });
  q.addEventListener('focus', () => { if (q.value.trim()) open(true); });
  $('#search-form').addEventListener('submit', (e) => { e.preventDefault(); open(true); search(q.value); });
  $('#search-form').addEventListener('keydown', (e) => { if (e.key === 'Escape') { panel.hidden = true; q.blur(); } });
  $('#search-cancel').addEventListener('click', () => { q.value = ''; search(''); toTab('now'); });
  document.addEventListener('pointerdown', (e) => {
    if (!e.target.closest('#search-panel, #search-form') && !matchMedia('(max-width: 600px)').matches) panel.hidden = true;
    if (!e.target.closest('.pop, #add-btn, #menu-btn, #settings-btn')) closePops();
  });
  // Openers
  for (const b of document.querySelectorAll('[data-op]')) b.addEventListener('click', () => { opTab = b.dataset.op; $('#op-find').value = ''; renderOpeners(last); });
  $('#op-find').addEventListener('input', () => last && renderOpeners(last));
  // Phone tabs
  for (const b of document.querySelectorAll('.tabbar button')) b.addEventListener('click', () => {
    toTab(b.dataset.tab);
    if (b.dataset.tab === 'search') q.focus();
  });
  // Queue reorder: pointer-based drag (sortable.js) -> POST /api/queue with pin = the dragged song.
  // Re-renders wait while dragging, so the list isn't rebuilt under the finger.
  const ord = (n) => (n % 100 >= 11 && n % 100 <= 13 ? `${n}th` : `${n}${['th', 'st', 'nd', 'rd'][n % 10] ?? 'th'}`);
  sortable($('#queue'), {
    onStart: () => { dragging = true; },
    onEnd: () => { dragging = false; },
    onMove: (gap, index) => { gap.textContent = `Drop here: plays ${ord(index + 1)}`; },
    onDrop: (id, index) => {
      if (!last) return;
      const ids = last.queue.map((x) => x.id);
      if (ids.indexOf(id) === index) { flush(); return; } // dropped where it was
      reorder(ids, id, index);
    },
  });
  // A select closing (or losing focus) releases a deferred render.
  document.addEventListener('focusout', (e) => { if (e.target.tagName === 'SELECT') setTimeout(flush); });

  // Add by link: the popover, or drop / paste a link anywhere on the page.
  $('#add-btn').addEventListener('click', () => togglePop('add'));
  $('#settings-btn').addEventListener('click', () => togglePop('settings'));
  $('#menu-btn').addEventListener('click', () => togglePop('menu'));
  $('#menu-add').addEventListener('click', () => togglePop('add', true));
  $('#menu-settings').addEventListener('click', () => togglePop('settings', true));
  for (const [name, p] of Object.entries(POPS)) {
    $(p.pop).addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      togglePop(name, false);
      // Back to whichever header button is on screen (desktop button or the phone's "…").
      [...p.btns, '#menu-btn'].map((b) => $(b)).find((b) => b.offsetParent)?.focus();
    });
  }
  $('#lastfm-form').addEventListener('submit', (e) => { e.preventDefault(); saveKey($('#lastfm-key').value.trim()); });
  $('#lastfm-remove').addEventListener('click', () => saveKey(''));
  $('#add-form').addEventListener('submit', (e) => { e.preventDefault(); add($('#url').value.trim()); });
  const overlay = $('#drop-overlay');
  const isUrlDrag = (e) => !e.dataTransfer.types.includes(ROW_TYPE)
    && (e.dataTransfer.types.includes('text/uri-list') || e.dataTransfer.types.includes('text/plain'));
  window.addEventListener('dragover', (e) => { if (isUrlDrag(e)) { e.preventDefault(); overlay.hidden = false; } });
  window.addEventListener('dragleave', (e) => { if (!e.relatedTarget) overlay.hidden = true; });
  window.addEventListener('drop', (e) => {
    overlay.hidden = true;
    if (!isUrlDrag(e)) return;
    e.preventDefault();
    togglePop('add', true);
    add(urlFrom(e.dataTransfer));
  });
  // Pasting a link outside any text field adds it directly; pasting into a field just fills it.
  document.addEventListener('paste', (e) => {
    if (e.target.closest?.('input, textarea')) return;
    const url = urlFrom(e.clipboardData);
    if (!/^https?:\/\//.test(url)) return;
    e.preventDefault();
    togglePop('add', true);
    add(url);
  });
}
