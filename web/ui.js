// Everything that isn't audio: API calls, the sources panel (Vibe / Party dropdowns; the phone's
// genre, mood and party chips), Up next, Suggestions / Played / Pairs / Library, search, "Or start with", add-by-link, notices and phone tabs.
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
// decks: which deck letter plays a song / takes the next one (app.js knows; the playlist rows say it).
let hooks = { loadOpener: () => {}, isLive: () => false, decks: { of: () => null, next: () => null } };
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
/** The active source in a few words: "Hits party", "Balkan brass", "Pop", "Any". */
const srcName = (s) => (s.playlist ? s.playlist.name : s.party ? `${cap(s.party)} party` : s.genre);
export const ordinal = (n) => (n % 100 >= 11 && n % 100 <= 13 ? `${n}th` : `${n}${['th', 'st', 'nd', 'rd'][n % 10] ?? 'th'}`);
/** Status badge: icon + word, so status never rests on colour alone. */
function badge(kind, text = cap(kind), title = '') {
  const spin = kind === 'analyzing' || kind === 'downloading';
  return el('span', { className: `badge ${kind}`, title: title || text },
    icon(STATUS_ICON[kind] ?? { auto: 'sparkles', pinned: 'pin', locked: 'lock', loved: 'flame', avoided: 'thumbs-down', mixes: 'check' }[kind] ?? 'info', spin ? 'spin' : ''), text);
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

// --- mix memory (design xWVJy): pure helpers for the copy ---------------------------------
/** The memory of the hand-over a -> b (pairs.json entry) or undefined. Key format "a>b". */
export const pairOf = (pairs, a, b) => (a && b ? pairs?.[`${a}>${b}`] : undefined);
export const isLoved = (p) => (p?.loved ?? 0) > 0;
/** Avoided as a pair (not just its effect): never placed back to back. */
export const isAvoided = (p) => !!p?.avoided && p.why !== 'effect';
const fx = (k) => FX_LABEL[k] ?? k;
/** Up next, under a loved partner: "You loved it after Despacito · Bass swap · 3×". */
export function lovedLine(prevTitle, p) {
  return [`You loved it after ${prevTitle}`, p.effect ? fx(p.effect) : null, p.loved > 1 ? `${p.loved}×` : null]
    .filter(Boolean).join(' · ');
}
/** Up next, after the song an avoid kept it away from. */
export const movedNote = (m) => `${m.title} moved ${m.by} later: you 👎 it right after ${m.after_title}.`;
/** Pairs tab: why an avoided pair is avoided. */
export const pairReason = (p) => (p.why === 'effect' && p.effect ? `${fx(p.effect)} was wrong for them` : "songs don't go together");
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
/** When a pair was last rated, the board's way: "tonight", "last Sat, 22:14", "2 weeks ago",
 * "last month". `now` is a Date (tests pass one). Same calendar day = tonight: it's a DJ app. */
export function pairWhen(iso, now = new Date()) {
  const t = iso ? new Date(iso) : null;
  if (!t || Number.isNaN(t.getTime())) return '';
  const day = (d) => Math.floor((d.getTime() - d.getTimezoneOffset() * 60000) / 86400000);
  const days = day(now) - day(t);
  if (days <= 0) return 'tonight';
  if (days === 1) return 'last night';
  const hm = `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`;
  if (days < 7) return `last ${DAYS[t.getDay()]}, ${hm}`;
  if (days < 30) return days < 14 ? 'last week' : `${Math.floor(days / 7)} weeks ago`;
  return days < 60 ? 'last month' : `${Math.floor(days / 30)} months ago`;
}
/** Pairs tab sub-line: "Bass swap · last Sat, 22:14" / "songs don't go together · tonight". */
export const pairSub = (p, now) => [p.avoided ? pairReason(p) : p.effect ? fx(p.effect) : null, pairWhen(p.last, now)]
  .filter(Boolean).join(' · ');
/** Effect picker: Auto's label, and an effect the owner 👎'd for this pair. */
export const autoLabel = (kind, lovedFx, short = false) => `Auto · ${fx(lovedFx || kind)}${lovedFx && !short ? ' (you loved it)' : ''}`;
export const fxOptionLabel = (k, avoidFx) => (k === avoidFx ? `${fx(k)} · you 👎 it here` : fx(k));
/** Copy with 👎 in it, as DOM nodes: the emoji drawn as the outline icon the design uses. */
export const withThumbs = (text) => text.split('👎').flatMap((part, i) => (i ? [icon('thumbs-down', 'thumb-i'), part] : [part]));

// --- Up next --------------------------------------------------------------------
/** Up next row kind: the owner's (kept) or an auto pick, with the note under the title. Pure. */
export function upNextNote(song, s) {
  if (song.kept) return { auto: false, sub: [song.channel, song.kept_from ? `kept from ${song.kept_from}` : null].filter(Boolean).join(' · ') };
  // A playlist's auto picks are its own songs; library songs fill in only around it (downloading, or the end).
  const from = s.source?.kind === 'playlist' ? (song.in_playlist ? s.source.label : 'library')
    : s.notice ? 'any genre' : s.source?.label ?? 'whole library';
  return { auto: true, badge: `Auto · ${from}`, sub: song.channel };
}

function queueRow(song, i, s, ids, prevId) {
  const locked = i < (s.queue_locked ?? 0);
  const note = upNextNote(song, s);
  const pair = pairOf(s.pairs, prevId, song.id);
  const loved = isLoved(pair);
  const li = el('li', { className: `row${locked ? ' locked' : ''}${loved ? ' loved' : ''}${note.auto ? ' auto' : ''}` },
    // Locked rows (already planned) have a lock instead of a grip: sortable.js only lifts grips.
    locked ? el('span', { className: 'lead', title: 'Locked in: already planned' }, icon('lock'), el('span', { className: 'sr-only', textContent: 'Locked in.' }))
      : el('span', { className: 'lead grip', title: 'Drag to reorder', 'aria-hidden': 'true' }, icon('grip-vertical')),
    thumb(song),
    el('span', { className: 'text' },
      el('span', { className: 't' }, song.title, loved ? ' ' : null, loved ? icon('flame') : null),
      loved ? el('span', { className: 's why', textContent: lovedLine(titleOf(prevId), pair) })
        // A locked auto pick shows the Locked badge, so its "Auto · …" note moves under the title.
        : el('span', { className: 's', textContent: [note.sub, note.auto && locked ? note.badge : null].filter(Boolean).join(' · ') })),
    el('span', { className: 'meta', textContent: songMeta(song), title: 'BPM  key  danceability' }),
    loved ? badge('loved', 'Loved after this') : isAvoided(pair) ? badge('avoided', 'Avoided pair')
      : locked ? badge('locked', 'Locked in') : note.auto ? badge('auto', note.badge, 'Fills in so the music never stops') : badge(song.status, cap(song.status), song.error),
    genreSelect(song, s.genres, `Genre for ${song.title}`),
    i ? deckBtn(song) : null, // the first row is already next
    el('span', { className: 'acts' },
      // ✓ keeps an auto pick: it becomes the owner's, noted as kept from the active source.
      note.auto ? iconBtn('check', `Keep ${song.title} in Up next`, () => act('/api/suggestions/accept', 'POST', { id: song.id, artist: '', title: song.title }), 'keep') : null,
      // Buttons too: drag-and-drop alone is not keyboard accessible.
      locked ? null : iconBtn('arrow-up', `Move ${song.title} up`, () => nudge(song.id, -1)),
      locked ? null : iconBtn('arrow-down', `Move ${song.title} down`, () => nudge(song.id, 1)),
      iconBtn('x', `Remove ${song.title}`, () => act(`/api/song/${song.id}`, 'DELETE'), 'remove'),
      moreBtn(song)));
  li.dataset.id = song.id;
  li.pick = { song, list: 'queue', ctx: { i, locked: s.queue_locked ?? 0, kept: song.kept } };
  const [up, down] = li.querySelectorAll('.acts .icon-btn:not(.keep, .more)');
  if (!locked) { up.disabled = i <= (s.queue_locked ?? 0); down.disabled = i === ids.length - 1; }
  return li;
}

function renderQueue(s) {
  const ids = s.queue.map((q) => q.id);
  const rows = s.queue.map((q, i) => queueRow(q, i, s, ids, i ? s.queue[i - 1].id : s.playing));
  // "X moved 3 later: you 👎 it right after Y" goes under Y's row (at the top when Y is playing).
  // No data-id, so sortable.js doesn't count it as a row.
  for (const m of s.moved ?? []) {
    const at = m.after === s.playing ? 0 : rows.findIndex((r) => r.dataset.id === m.after) + 1;
    if (at > 0 || m.after === s.playing) rows.splice(at, 0, el('li', { className: 'moved-note' }, icon('thumbs-down'), el('span', {}, withThumbs(movedNote(m)))));
  }
  $('#queue').replaceChildren(...rows);
  // "Play next" picks still downloading / analysing: shown first, not draggable yet.
  $('#requested').replaceChildren(...(s.requested ?? []).map((q) => el('li', { className: 'row' },
    el('span', { className: 'lead' }, icon('list-start')),
    thumb(q),
    el('span', { className: 'text' }, el('span', { className: 't', textContent: q.title }),
      el('span', { className: q.status === 'failed' ? 's err' : 's', textContent: q.error
        || ['Plays next once ready', q.kept_from ? `kept from ${q.kept_from}` : null].filter(Boolean).join(' · ') })),
    badge(q.status, cap(q.status), q.error),
    el('span', { className: 'acts' }, iconBtn('x', `Remove ${q.title}`, () => act(`/api/song/${q.id}`, 'DELETE'), 'remove')))));
  $('#queue-empty').hidden = s.queue.length + (s.requested?.length ?? 0) > 0;
  $('#queue-empty').textContent = s.pool_counts?.ready ? 'Nothing left to play. Keep songs from Suggestions or search.'
    : 'Nothing is ready yet. Songs join Up next as they finish preparing.';
  const c = s.pool_counts, working = (c.analyzing ?? 0) + (c.downloading ?? 0);
  const what = srcName(s);
  $('#queue-count').textContent = [what, `${c.ready ?? 0} ready`, working ? `${working} analyzing` : null,
    c.queued ? `${c.queued} queued` : null].filter(Boolean).join(' · ');
  const total = Math.max(1, s.pool.length);
  $('#queue-progress .r').style.width = `${((c.ready ?? 0) / total) * 100}%`;
  $('#queue-progress .w').style.width = `${(working / total) * 100}%`;
}

// --- play next on deck B (design yA49d) ------------------------------------------------
// The deck a pick lands on right now ({deck: 0|1, afterMix}), kept current by app.js (setPick).
let pickAt = { deck: 1, afterMix: false };
const letter = () => 'AB'[pickAt.deck];
/** app.js, every frame: relabel the deck buttons when the target deck changes (swap, a mix
 * starting). Touches the DOM only on a change. */
export function setPick(p) {
  if (p.deck === pickAt.deck && p.afterMix === pickAt.afterMix) return;
  pickAt = p;
  for (const b of document.querySelectorAll('.deck-btn')) paintDeckBtn(b);
}
function paintDeckBtn(b) {
  const d = letter();
  b.replaceChildren(el('span', { className: `letter ${d.toLowerCase()}`, 'aria-hidden': 'true', textContent: d }),
    el('span', { className: 'lbl', textContent: 'Play next' }));
  b.setAttribute('aria-label', `Play ${b.dataset.title} next on deck ${d}`);
  b.title = pickAt.afterMix ? `Plays right after this mix, on deck ${d}` : `Next on deck ${d}`;
}
/** The deck button: a tile in the target deck's colour + "Play next". Same everywhere. */
function deckBtn(song, where = 'list') {
  const b = el('button', { type: 'button', className: `deck-btn ${where}` });
  b.dataset.title = song.title;
  b.onclick = () => playNext(song, b);
  paintDeckBtn(b);
  return b;
}
// Phone: the row's actions live in a sheet (long-press, or this button).
const moreBtn = (song) => iconBtn('ellipsis', `More for ${song.title}`, (e) => openSheet(e.currentTarget.closest('li').pick), 'more');

/** The one line above Up next after a pick. Pure. */
export function pickNote({ title, deck, afterMix = false, ready, planned }) {
  if (!ready) return `${title} goes on deck ${deck} once it’s analysed.${planned ? ` Until then ${planned} stays planned.` : ''}`;
  if (afterMix) return `${title} plays after this mix, on deck ${deck}.`;
  return `${title} is next on deck ${deck}.${planned ? ` ${planned} moved back to the top of Up next.` : ''}`;
}
// The note above Up next, in the target deck's colour (blue while the pick is still being prepared).
let picked = null; // {id, wait, kind: 'a' | 'b' | 'working', text, until}
async function playNext(song, btn = null) {
  const at = { ...pickAt }, d = letter();
  // A search hit not in the library yet is fetched first: a spinner in place of the tile.
  const search = btn?.classList.contains('search');
  if (search && !song.status) btn.replaceChildren(icon('loader', 'spin'));
  try {
    const r = await api('/api/play_next', 'POST', { id: song.id, title: song.title, channel: song.channel || '',
      duration: song.duration || 0, source: search ? 'search' : null });
    const { ready, planned } = r.play_next;
    // ready = it's first in Up next. Not ready waits (no time limit) until it leaves `requested`.
    picked = { id: song.id, wait: !ready, kind: ready ? d.toLowerCase() : 'working', until: Date.now() + 12000,
      text: pickNote({ title: song.title, deck: d, afterMix: at.afterMix, ready, planned: planned && titleOf(planned) }) };
    if (search) {
      $('#search-msg').textContent = ready ? `${song.title} is next on deck ${d}.` : `Preparing "${song.title}"… it plays next once ready.`;
    }
    setState(r);
    renderPickNote(r); // shows even when the state didn't change (the pick was next already)
    setTimeout(() => last && renderPickNote(last), 12500);
  } catch (e) { report(e); if (btn) paintDeckBtn(btn); }
}
function renderPickNote(s) {
  const n = $('#pick-note');
  // Waiting: gone once it swaps in (or was removed). Done: gone after a while (the next render).
  if (picked && (picked.wait ? !s.requested?.some((q) => q.id === picked.id) : Date.now() > picked.until)) picked = null;
  n.hidden = !picked;
  if (!picked) return;
  n.className = `banner pick ${picked.kind}`;
  n.replaceChildren(icon(picked.wait ? 'loader' : 'circle-check', picked.wait ? 'spin' : ''), el('span', { textContent: picked.text }));
}

/** The phone action sheet's actions for a row: only what applies to that list. Pure. */
export function sheetActions(list, { i = 0, locked = 0, kept = false } = {}) {
  const out = [];
  if (list !== 'queue' || i > 0) out.push({ id: 'next' }); // the first row is already next
  if (list === 'queue') {
    if (i > locked) out.push({ id: 'top' });
    if (i >= locked && !kept) out.push({ id: 'pin' });
    out.push({ id: 'remove' });
  }
  return out;
}
function openSheet(p) {
  if (!p) return;
  const { song, list, ctx } = p, dlg = $('#sheet');
  const close = () => dlg.close();
  const ids = () => last.queue.map((q) => q.id);
  const ACT = {
    next: [`Play next on deck ${letter()}`, null, () => playNext(song), 'deck'],
    top: ['Move to top', 'arrow-up-to-line', () => reorder(ids(), song.id, last.queue_locked ?? 0)],
    // Pin = the owner placed it here: it stops being an auto pick and keeps its spot.
    pin: ['Pin here', 'pin', () => reorder(ids(), song.id, ids().indexOf(song.id))],
    // Same as the row's ✕: drops it from the library (files stay, re-adding is instant). The board
    // says "Remove from Up next", but there's no queue-only removal, so the label says what happens.
    remove: ['Remove from library', 'x', () => act(`/api/song/${song.id}`, 'DELETE'), 'danger'],
  };
  const sub = [song.channel, song.bpm ? `${Math.round(song.bpm)} BPM` : null, song.status ? cap(song.status) : null].filter(Boolean).join(' · ');
  dlg.replaceChildren(el('div', { className: 'sheet-head' }, thumb(song),
    el('span', { className: 'text' }, el('b', { textContent: song.title }), el('small', { textContent: sub }))),
  ...sheetActions(list, ctx).map(({ id }) => {
    const [label, ic, fn, cls = ''] = ACT[id];
    return el('button', { type: 'button', className: `mi sheet-act ${cls}`, onclick: () => { close(); fn(); } },
      ic ? icon(ic) : el('span', { className: `letter ${letter().toLowerCase()}`, textContent: letter() }), el('span', { textContent: label }));
  }));
  dlg.showModal();
}

// --- render -------------------------------------------------------------------------
function render(s) {
  const moods = s.moods ?? {};
  renderGenres(s, moods);
  $('#b2b').setAttribute('aria-checked', String(!!s.beat_to_beat));
  renderParty(s);
  renderMoods(s, moods);
  renderSources(s);
  renderPlChips(s);
  renderPlHead(s);
  const notice = $('#notice');
  // While a playlist plays its notices (the end is near, songs still downloading) sit in its panel.
  notice.hidden = !s.notice || s.source?.kind === 'playlist';
  notice.querySelector('span').textContent = s.notice ?? '';
  renderPickNote(s);
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
    type: 'button', textContent: g, className: 'chip', 'aria-pressed': String(g === s.genre && !s.party && !s.playlist),
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

async function deleteMood(s, name, n) {
  const wasOn = s.genre === name;
  await act(`/api/mood/${encodeURIComponent(name)}`, 'DELETE');
  moodReport('info', `Removed ${name}. Its ${n} songs stay in your library.${wasOn ? ' Back to Any.' : ''}`);
}

function renderMoods(s, m) {
  const chips = Object.entries(m).filter(([name]) => !building.has(name)).map(([name, n]) => {
    const on = s.genre === name && !s.party;
    return el('span', { className: `mood-chip${on ? ' on' : ''}` },
      el('button', { type: 'button', className: 'name', 'aria-pressed': String(on),
        onclick: () => act('/api/genre', 'POST', { genre: name }) }, name, el('span', { className: 'n', textContent: String(n) })),
      iconBtn('refresh-cw', `Refresh mood ${name}: add more songs from the playlists`, () => buildMood(name)),
      iconBtn('x', `Delete mood ${name} (songs stay)`, () => deleteMood(s, name, n)));
  });
  const progress = [...building.keys()].map((name) => el('span', { className: 'mood-chip building', role: 'status' },
    icon('loader', 'spin'), name, el('small', { textContent: 'reading playlists…' })));
  $('#mood-list').replaceChildren(...progress, ...chips);
}

// --- suggestions / played / pairs / library ---------------------------------------------
const TABS = ['sugg', 'played', 'pairs', 'library'];
let logTab = 'sugg', histFor, pairsFor, pairFilter = 'all', libFilter = 'all';

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
  for (const t of TABS) {
    const on = t === name;
    $(`#tab-${t}`).setAttribute('aria-selected', String(on));
    $(`#tab-${t}`).tabIndex = on ? 0 : -1;
    $(`#pane-${t}`).hidden = !on;
  }
  histFor = pairsFor = undefined; // reload what the tab shows
  if (last) renderLogs(last);
}

const FLAME = 'flame', THUMB = 'thumbs-down';
/** POST a pair action, render the new state, then refresh the open list. */
async function pairAct(path, body) {
  try {
    setState(await api(path, 'POST', body));
    if (logTab === 'played') loadHistory();
  } catch (e) { report(e); }
}

async function loadHistory() {
  try {
    const { history } = await api('/api/history?limit=300');
    $('#history').replaceChildren(...history.flatMap((h) => {
      const v = h.verdict, pair = { from: h.from, to: h.id };
      // Tapping the pressed one undoes it (the server keeps what the pair was before).
      const rateBtn = (kind, ic, label) => {
        const b = iconBtn(ic, label, () => (v === kind ? pairAct('/api/pair/undo', pair)
          : pairAct('/api/pair', { ...pair, verdict: kind, effect: h.effect })), `rate ${kind === 'loved' ? 'love' : 'avoid'}`);
        b.setAttribute('aria-pressed', String(v === kind));
        return b;
      };
      const li = el('li', { className: 'logrow' },
        el('span', { className: 'at', textContent: (h.at || '').slice(11, 16) }),
        thumb(h),
        el('span', { className: 't', textContent: h.title }),
        h.from ? el('span', { className: 'from mono', textContent: `from ${titleOf(h.from)}` }) : null,
        el('span', { className: 'fx' }, h.effect ? [icon(FX_ICON[h.effect] ?? 'blend'), FX_LABEL[h.effect] ?? h.effect] : 'Started'),
        // A row rates the hand-over INTO this song; a song started from scratch has none.
        h.from ? rateBtn('loved', FLAME, `Nailed it: the mix from ${titleOf(h.from)} into ${h.title}`) : el('span', { className: 'rate' }),
        h.from ? rateBtn('avoided', THUMB, `Didn't work: the mix from ${titleOf(h.from)} into ${h.title}`) : el('span', { className: 'rate' }),
        // Not for the playing song (always the newest row): it can't be next as well.
        h.id === last?.playing ? null : [deckBtn(h), moreBtn(h)]);
      if (h.id !== last?.playing) li.pick = { song: h, list: 'played', ctx: {} };
      // 👎 asks one optional question inline, so the right thing gets avoided. Unanswered it
      // counts as "songs" (the planner treats no reason like that).
      const ask = v === 'avoided' && !h.why ? el('li', { className: 'why-ask' },
        el('p', { className: 'label', textContent: 'What was off?' }),
        el('div', { className: 'seg-chips' },
          el('button', { type: 'button', className: 'chip', onclick: () => pairAct('/api/pair/why', { ...pair, why: 'songs' }) },
            "These songs don't go together"),
          el('button', { type: 'button', className: 'chip', onclick: () => pairAct('/api/pair/why', { ...pair, why: 'effect' }) },
            'Wrong effect for them'))) : null;
      return ask ? [li, ask] : [li];
    }));
    $('#history-empty').hidden = history.length > 0;
  } catch (e) { report(e); }
}

async function loadPairs() {
  try {
    const { pairs } = await api('/api/pairs'); // most loved first (server), avoided last
    const n = { all: pairs.length, loved: pairs.filter((p) => !p.avoided).length };
    n.avoided = n.all - n.loved;
    $('#pair-filter').replaceChildren(...['all', 'loved', 'avoided'].map((k) => el('button', { type: 'button', className: 'chip',
      'aria-pressed': String(pairFilter === k), onclick: () => { pairFilter = k; pairsFor = undefined; renderLogs(last); } },
    `${cap(k)} ${n[k]}`)), el('span', { className: 'hint sort', textContent: 'Most loved first' }));
    const shown = pairs.filter((p) => pairFilter === 'all' || (pairFilter === 'avoided') === !!p.avoided);
    const now = new Date();
    $('#pairs').replaceChildren(...shown.map((p) => {
      const pair = { from: p.from, to: p.to }, name = `${p.from_title} → ${p.to_title}`;
      return el('li', { className: 'logrow pair' },
        el('span', { className: 'text' },
          el('span', { className: 't pair-t' }, p.from_title, icon('arrow-right'), p.to_title),
          // A pair migrated from the first pair memory has no effect or time: no sub-line.
          pairSub(p, now) ? el('span', { className: 's' }, icon(p.avoided && p.why !== 'effect' ? 'ban' : FX_ICON[p.effect] ?? 'sparkles'), pairSub(p, now)) : null),
        p.avoided ? badge('avoided', 'avoid') : el('span', { className: 'badge loved', title: `${p.loved} × nailed it` }, icon(FLAME), `×${p.loved}`),
        // Play pair: both songs are the very next ones, in this order (play_next machinery).
        p.avoided ? null : el('button', { type: 'button', className: 'btn', onclick: () => pairAct('/api/pair/play', pair) },
          icon('list-start'), 'Play pair'),
        iconBtn('x', `Forget ${name}`, () => pairAct('/api/pair/forget', pair), 'remove'));
    }));
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
  $('#pool').replaceChildren(...rows.map((p) => libRow(p, s)));
}
function libRow(p, s) {
  // A failed song can't play: Retry is its action. The playing song is already on a deck.
  const can = p.status !== 'failed' && p.id !== s.playing;
  const li = el('li', { className: 'logrow' }, thumb(p),
    el('span', { className: 'text' }, el('span', { className: 't', textContent: p.title }),
      el('span', { className: p.status === 'failed' ? 's err' : 's', textContent: p.status === 'failed' ? p.error || 'Failed' : p.channel })),
    badge(p.status, cap(p.status), p.error),
    p.status === 'failed' ? el('button', { type: 'button', className: 'btn', onclick: () => act(`/api/song/${p.id}/retry`, 'POST') }, icon('rotate-ccw'), 'Retry')
      : genreSelect(p, s.genres, `Genre for ${p.title}`),
    can ? deckBtn(p) : null,
    iconBtn('x', `Remove ${p.title}`, () => act(`/api/song/${p.id}`, 'DELETE'), 'remove'),
    can ? moreBtn(p) : null);
  if (can) li.pick = { song: p, list: 'library', ctx: {} };
  return li;
}

// --- party mode ---------------------------------------------------------------
const FLAVOR_LABEL = { hits: 'Hits', oldies: 'Oldies', latino: 'Latino' };

// --- sources (Vibe / Party dropdowns, design mNazX) ------------------------------------
/** "Playing from" sentence for state.source (null = Any with party off). */
export function sourceSentence(src, pl = null) {
  if (!src) return 'Next songs come from your whole library, auto-ordered.';
  if (src.kind === 'playlist') return `Playing the ${src.name} playlist, ${pl?.order === 'mix' ? 'in mix order' : 'in order'}.`;
  if (src.kind === 'party') return 'Party is on: crowd-pleasers, energy building.';
  return `Next songs come from the ${src.name} ${src.kind}.`;
}
/** What each dropdown shows. The active one (the source that's on) gets the outline and red dot;
 * the other shows its default (Any / Off) dimmed. */
export function sourceSelects(s) {
  const party = s.party ?? null, pl = s.playlist ?? null;
  return {
    vibe: { value: party || pl ? 'Any' : s.genre, active: !party && !pl && s.genre !== 'Any' },
    party: { value: party ? FLAVOR_LABEL[party] ?? cap(party) : 'Off', active: !!party },
    playlist: { value: pl ? pl.name : 'None', active: !!pl },
  };
}
/** The line over the draft. */
export function draftNote(src) {
  if (!src) return 'Pick a mood, genre or party to get suggestions';
  if (src.kind === 'party') return `Drafted by Party: ${src.label}. ✓ adds to Up next, ✕ never suggests it in ${src.label} again.`;
  return `Drafted from ${src.label}. ✓ adds to Up next, ✕ never suggests it here again.`;
}
// Party copy is the design's (TTMRy 02).
const PARTY_OPTS = [[null, 'Off', 'Vibe or playlist decides'], ['hits', 'Hits', 'Big songs everyone knows'],
  ['oldies', 'Oldies', '60s to 90s classics'], ['latino', 'Latino', 'Reggaeton, salsa, bachata']];

function renderSources(s) {
  const sel = sourceSelects(s);
  for (const k of MENUS) {
    const b = $(`#src-${k}`);
    b.classList.toggle('on', sel[k].active);
    b.querySelector('.v span').textContent = sel[k].value;
  }
  $('#src-line').textContent = sourceSentence(s.source, s.playlist);
  // An open menu is rebuilt so a mood that finished building (or was deleted) shows up.
  if (!$('#vibe-menu').hidden) buildVibeMenu(s);
  // (Not while the owner types in its Add field: a rebuild would eat the text.)
  if (!$('#playlist-menu').hidden && !document.activeElement?.matches('#playlist-menu :is(input, select)')) buildPlaylistMenu(s);
}

function option(label, desc, on, onclick, extra = [], lead = null) {
  return el('div', { className: `opt${on ? ' on' : ''}` },
    el('button', { type: 'button', className: 'pick', 'aria-pressed': String(on), onclick }, lead,
      el('span', { className: 'ot' }, el('b', { textContent: label }), desc ? el('small', { textContent: desc }) : null)),
    ...extra, on ? icon('check', 'check') : null);
}
const pick = (path, body) => () => { closeMenus(); act(path, 'POST', body); };

function buildVibeMenu(s) {
  const moods = s.moods ?? {}, on = (g) => !s.party && s.genre === g;
  const plain = s.genres.filter((g) => !(g in moods));
  $('#vibe-menu').replaceChildren(
    option('Any', 'Whole library, auto-ordered', on('Any'), pick('/api/genre', { genre: 'Any' })),
    plain.length ? el('p', { className: 'label grp', textContent: 'Genres' }) : null,
    ...plain.map((g) => option(g, '', on(g), pick('/api/genre', { genre: g }))),
    Object.keys(moods).length || building.size ? el('p', { className: 'label grp', textContent: 'Moods' }) : null,
    ...[...building.keys()].map((name) => el('div', { className: 'opt building', role: 'status' },
      icon('loader', 'spin'), el('b', { textContent: name }), el('small', { textContent: 'reading playlists…' }))),
    ...Object.entries(moods).filter(([name]) => !building.has(name)).map(([name, n]) => option(name, '', on(name),
      pick('/api/genre', { genre: name }), [el('span', { className: 'n', textContent: String(n) }),
        iconBtn('refresh-cw', `Refresh mood ${name}: add more songs from the playlists`, () => { closeMenus(); buildMood(name); }, 'hov'),
        iconBtn('x', `Delete mood ${name} (songs stay)`, () => { closeMenus(); deleteMood(s, name, n); }, 'hov remove')])),
    el('button', { type: 'button', className: 'opt-add', onclick: () => { closeMenus(); openComposer(); } }, icon('plus'), 'Build a mood'));
}
function buildPartyMenu(s) {
  $('#party-menu').replaceChildren(...PARTY_OPTS.map(([f, label, desc]) =>
    option(label, desc, (s.party ?? null) === f, pick('/api/party', { flavor: f }))));
}
const MENUS = ['vibe', 'party', 'playlist'];
function toggleMenu(name, open) {
  const menu = $(`#${name}-menu`), btn = $(`#src-${name}`);
  open ??= menu.hidden;
  if (open) { closeMenus(); ({ vibe: buildVibeMenu, party: buildPartyMenu, playlist: buildPlaylistMenu })[name](last); }
  menu.hidden = !open;
  btn.setAttribute('aria-expanded', String(open));
  if (open) (menu.querySelector('.opt.on .pick') ?? menu.querySelector('.pick'))?.focus();
}
function closeMenus() {
  for (const n of MENUS) { $(`#${n}-menu`).hidden = true; $(`#src-${n}`).setAttribute('aria-expanded', 'false'); }
  if (plMenu.where === 'drop') plMenu = {};
}

// --- draft (Suggestions tab, design j2Exi) -------------------------------------------------
export const UNDO_MS = 5000;
/** Draft rows: {x, state: 'new' | 'working' | 'kept' | 'excluded', until}. An excluded row shows
 * Undo until `until`, then leaves the list. Pure, so the timer logic is testable. */
export function sweepDraft(rows, now) {
  return rows.filter((r) => !(r.state === 'excluded' && r.until <= now));
}
/** Add all N: the rows nobody has decided on yet, in the order shown. */
export const pendingRows = (rows) => rows.filter((r) => r.state === 'new');
/** Every row decided (kept, or excluded and its Undo ran out): the "all reviewed" state. */
export const allReviewed = (rows, total) => total > 0 && rows.every((r) => r.state === 'kept');

let draft = { for: null, src: null, rows: [], total: 0, kept: 0, excluded: 0, loading: false, error: null };
let sweepTimer = 0;
// Reloaded when the source changes, a song starts (it left Up next / joined Played; party picks
// follow what's playing) or a mood is refreshed.
// A playlist's rows follow every change (songs getting ready, next song planned, a sync).
const draftKey = (s) => (!s.source ? '' : s.source.kind === 'playlist' ? `${s.source.key}|${s.playing}|${s.version}`
  : `${s.source.key}|${s.playing}|${s.moods?.[s.source.name] ?? ''}`);

function renderParty(s) {
  // Phone's Vibe tab: the segmented control, as before.
  $('#party-btns').replaceChildren(...[null, ...(s.flavors ?? [])].map((f) => el('button', {
    type: 'button', textContent: f ? FLAVOR_LABEL[f] ?? f : 'Off', 'aria-pressed': String((s.party ?? null) === f),
    onclick: () => act('/api/party', 'POST', { flavor: f }),
  })));
  $('#party-hint').replaceChildren(el('span', { className: 'phone-only', textContent: s.party
    ? 'Crowd-pleasers first, energy building. Genres and moods are paused while Party is on.'
    : 'Off: your genre or mood picks the songs. Hits, Oldies and Latino play crowd-pleasers and pause genres and moods.' }));
  const k = draftKey(s);
  if (k !== draft.for) { draft.for = k; loadDraft(); }
}

async function loadDraft() {
  const want = draft.for;
  // The same playlist reloading (a song got ready, the next one changed): keep its rows on screen,
  // and any ✕ still showing Undo, instead of flashing an empty "Drafting…" list.
  const same = last?.source?.kind === 'playlist' && draft.src?.key === last.source.key;
  const held = same ? draft.rows.filter((r) => r.state === 'excluded' && r.until > Date.now()) : [];
  if (!same) {
    draft = { ...draft, src: last?.source ?? null, rows: [], total: 0, kept: 0, excluded: 0, loading: !!last?.source, error: null };
    renderDraft();
  }
  if (!last?.source) return;
  try {
    const r = await api('/api/suggestions');
    if (draft.for !== want) return; // the source changed while Last.fm was answering
    const rows = r.suggestions.map((x) => ({ x, state: 'new' }));
    for (const h of held) if (!rows.some((q) => q.x.id === h.x.id)) rows.splice(Math.min(h.at, rows.length), 0, h);
    draft = { ...draft, src: r.source, playlist: r.playlist ?? null, rows, total: r.suggestions.length, loading: false, error: null };
  } catch (e) { draft = { ...draft, loading: false, error: e.message }; }
  renderDraft();
}

const thumb = (x) => {
  const t = el('span', { className: 'thumb', 'aria-hidden': 'true' }, icon('music'));
  if (x.id) {
    // No id = a Last.fm pick not on YouTube yet: the music-note tile stays.
    const img = el('img', { src: `https://i.ytimg.com/vi/${x.id}/mqdefault.jpg`, alt: '', loading: 'lazy' });
    img.onerror = () => img.remove();
    t.append(img);
  }
  return t;
};

function draftRow(r) {
  const { x } = r, src = draft.src;
  const kept = r.state === 'kept', out = r.state === 'excluded', working = r.state === 'working';
  const why = kept ? ['In Up next', x.artist].filter(Boolean).join(' · ')
    : out ? 'Won’t be suggested for this source again'
      : [x.artist, x.year, x.why, x.status && x.status !== 'ready' ? x.status : null].filter(Boolean).join(' · ');
  const body = { id: x.id ?? null, artist: x.artist, title: x.title, source: src?.key };
  const keep = async () => {
    r.state = 'working'; renderDraft();
    try {
      setState(await api('/api/suggestions/accept', 'POST', body));
      r.state = 'kept'; draft.kept++;
    } catch (e) { r.state = 'new'; report(e); }
    renderDraft();
  };
  const exclude = async () => {
    try { await api('/api/suggestions/reject', 'POST', body); } catch (e) { report(e); return; }
    r.state = 'excluded'; r.until = Date.now() + UNDO_MS; draft.excluded++;
    renderDraft();
    clearTimeout(sweepTimer);
    sweepTimer = setTimeout(sweep, UNDO_MS + 50);
  };
  const undo = async () => {
    try { await api('/api/suggestions/unreject', 'POST', body); } catch (e) { report(e); return; }
    r.state = 'new'; draft.excluded--; renderDraft();
  };
  const bpm = el('span', { className: `meta${x.mixes ? ' mixes' : ''}`, textContent: x.bpm ? x.bpm.toFixed(1) : '–',
    title: x.mixes ? 'BPM · beat-matches the current song' : x.bpm ? 'BPM' : 'Tempo unknown' });
  return el('li', { className: `row draft-row ${r.state}` }, thumb(x),
    el('span', { className: 'text' }, el('span', { className: 't', textContent: x.title }),
      el('span', { className: 's', textContent: why })),
    bpm, x.mixes && !kept && !out ? el('span', { className: 'sr-only', textContent: 'mixes with what’s playing' }) : null,
    out ? el('button', { type: 'button', className: 'btn undo', onclick: undo }, icon('undo-2'), 'Undo')
      : el('span', { className: 'acts' },
        working ? el('span', { className: 'badge working' }, icon('loader', 'spin'), x.id ? 'Adding…' : 'Finding on YouTube…')
          : el('button', { type: 'button', className: `icon-btn keep${kept ? ' on' : ''}`, disabled: kept,
            'aria-label': kept ? `${x.title} is in Up next` : `Keep ${x.title}: add it to Up next`, title: kept ? 'In Up next' : 'Add to Up next', onclick: keep }, icon('check')),
        kept || working ? null : el('button', { type: 'button', className: 'icon-btn excl', title: 'Never suggest it for this source',
          'aria-label': `Never suggest ${x.title} for ${src?.label ?? 'this source'}`, onclick: exclude }, icon('x'))));
}

function sweep() {
  draft.rows = sweepDraft(draft.rows, Date.now());
  renderDraft();
}

async function addAll() {
  const todo = pendingRows(draft.rows);
  if (!todo.length) return;
  for (const r of todo) r.state = 'working';
  renderDraft();
  try {
    setState(await api('/api/suggestions/accept_all', 'POST', { items: todo.map((r) => ({ id: r.x.id ?? null, artist: r.x.artist, title: r.x.title })) }));
    for (const r of todo) r.state = 'kept';
    draft.kept += todo.length;
  } catch (e) { for (const r of todo) if (r.state === 'working') r.state = 'new'; report(e); }
  renderDraft();
}

function renderDraft() {
  if (draft.src?.kind === 'playlist') { renderPlaylistDraft(); return; }
  $('#pl-notes').replaceChildren();
  const { src, rows } = draft, n = pendingRows(rows).length;
  const done = $('#sugg-done'), all = $('#sugg-all');
  all.hidden = !n;
  all.querySelector('span').textContent = `Add all ${n}`;
  const msg = $('#sugg-msg');
  if (draft.loading) msg.textContent = src?.kind === 'party' ? 'Finding suggestions…' : 'Drafting…';
  else if (draft.error) msg.textContent = draft.error;
  else msg.textContent = draftNote(src);
  const reviewed = allReviewed(rows, draft.total);
  const empty = !!src && !draft.loading && !draft.error && (reviewed || !rows.length);
  $('#tab-sugg .n').textContent = src ? String(empty ? 0 : rows.length) : '';
  $('#sugg').replaceChildren(...(empty ? [] : rows.map(draftRow)));
  done.hidden = !empty;
  msg.hidden = empty;
  if (!empty) return;
  if (src.kind === 'party' && !last?.lastfm && !draft.total) {
    // No Last.fm key: a setup hint instead of an empty list (party ordering still works).
    done.hidden = true; msg.hidden = false;
    msg.replaceChildren(el('span', { className: 'key-hint' },
      el('b', {}, icon('key-round'), ' Suggestions need a free Last.fm API key'),
      el('button', { type: 'button', className: 'btn', onclick: () => togglePop('settings', true) }, icon('settings'), 'Add it in Settings'),
      el('a', { href: 'https://www.last.fm/api/account/create', target: '_blank', rel: 'noopener' }, 'Get a key at last.fm/api')));
    return;
  }
  const [title, detail] = reviewed
    ? [`All ${draft.total} ${src.label} picks reviewed`, `${draft.kept} kept, ${draft.excluded} excluded. `]
    : [`Nothing new from ${src.label}`, src.kind === 'party' ? 'Every pick so far is in Up next or excluded. ' : `Every ${src.label} song is in Up next or excluded. `];
  // The one useful next step for the source (design TTMRy 08).
  const next = src.kind === 'mood' ? ['Refresh the mood to read the playlists again for new picks.', `Refresh ${src.label}`, () => buildMood(src.name)]
    : src.kind === 'party' ? ['New picks come as the music changes.', 'Find more', () => { draft.for = null; renderParty(last); }]
      : ['Build a mood to draft songs from public playlists.', 'Build a mood', () => openComposer()];
  done.querySelector('.t').textContent = title;
  done.querySelector('.d').textContent = detail + next[0];
  done.querySelector('.btn').replaceChildren(icon(src.kind === 'genre' ? 'plus' : 'refresh-cw'), next[1]);
  done.querySelector('.btn').onclick = next[2];
}


// --- saved playlists (design HJGfo) ---------------------------------------------------------
/** "2 h ago" for a unix time in seconds. */
export function ago(ts, now = Date.now() / 1000) {
  const m = Math.max(0, Math.floor((now - ts) / 60));
  return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
}
const songs = (n) => `${n} song${n === 1 ? '' : 's'}`;
/** "48 songs · 12 ready" / "86 songs · all ready" (the dropdown rows). */
export const plCount = (p) => `${songs(p.count)} · ${p.ready >= p.count ? 'all ready' : `${p.ready} ready`}`;
/** The head's line: counts and the last sync, or "N left" near the end (the at-end notice is due). */
export function plMeta(p) {
  if (p.left != null && p.left <= 2 && p.count > 2) return `${songs(p.count)} · ${p.left} left`;
  return `${plCount(p)} · synced ${ago(p.synced_at)}`;
}
/** A playlist row's second line (design 04/05). `decks.of(id)` = the deck letter playing it,
 * `decks.next()` = the deck the next song goes on. Pure. */
export function plRowNote(x, decks = { of: () => null, next: () => null }, isNew = false) {
  const n = `#${x.pos}`, tail = x.last ? ' · last one' : '';
  if (x.state === 'played') return `${n} · played ${x.played_at ?? ''}`.trim();
  if (x.state === 'playing') { const d = decks.of(x.id); return `${n} · now playing${d ? ` on ${d}` : ''}`; }
  if (x.state === 'failed') return `${n} · can’t play`;
  if (isNew) return `${n} · new on YouTube`;
  if (x.state === 'waiting') return `${n} · downloading, skipped for now`;
  if (x.plan != null) return `${n} → ${x.plan === 0 ? 'next' : ordinal(x.plan + 2)}${x.why ? ` · ${x.why}` : ''}${tail}`;
  if (x.state === 'next') { const d = decks.next(); return `${n} · next${d ? ` on deck ${d}` : ''}${tail}`; }
  if (x.state === 'kept') return `${n} · In Up next`;
  return [n, x.artist].filter(Boolean).join(' · ') + tail;
}
/** The ↻ result (design 07). */
export function syncNote(r) {
  const a = r.added.length;
  if (!a && !r.removed) return 'Synced: nothing changed on YouTube.';
  const added = a ? `${a} new ${a === 1 ? 'song' : 'songs'} added (${r.added.map((k) => `#${k}`).join(', ')}).` : 'no new songs.';
  const gone = r.removed ? ` ${r.removed} ${r.removed === 1 ? 'was' : 'were'} removed on YouTube: out of the playlist, still in your library.` : '';
  return `Synced: ${added}${gone}`;
}
export const savedNote = (r) => `${r.playlist.name} saved: ${songs(r.found)} found, ${r.added} new to the library. Downloading from the top.`;
export const removedNote = (r) => `Removed ${r.name}. Its ${songs(r.count)} stay in your library.${r.was_playing ? ' Playing from Vibe: Any.' : ''}`;
export const failedNote = (p) => `${songs(p.failed)} in ${p.name} can’t play${p.fail_reason ? ` (${p.fail_reason})` : ''}. They’re skipped; Retry is in Library.`;
const AT_END = [['keep_going', 'Keep going', 'Similar songs from the library, then back to Vibe: Any'],
  ['repeat', 'Repeat playlist', 'Start again from #1'], ['stop', 'Stop after last', 'Fade out, ON AIR turns off']];

let plSaving = 0;            // saves in flight: a "Reading playlist…" row each
let plNote = null;           // the dropdown's report: {kind, text, act?: [label, icon, fn]}
let plAdd = null;            // the Add field when open: {url, genre, err?, act?}
const plSyncing = new Set(); // ids being re-read
const plNew = {};            // id -> Set of song ids the last sync found ("new on YouTube")
let plSynced = null;         // {id, kind, text}: the last sync's report, shown in the playlist's panel
let plMenu = {};             // the open ⋯ menu: {id, where: 'drop' | 'head', sub?: 'genre'}
let plAtEnd = false;         // the "At the end" list is open
let renaming = null;         // id whose name is an input right now
const plFailTold = (id) => +(localStorage.getItem(`autodeck.plfail.${id}`) || 0);
const plFailShown = {};      // id -> count: "can't play" listed once per new failure, then quiet

/** Cover: the first four songs' thumbnails, 2 × 2 (design 03). */
function cover(ids, cls = '') {
  return el('span', { className: `cover ${cls}`.trim(), 'aria-hidden': 'true' }, ...[0, 1, 2, 3].map((k) => {
    const id = ids?.[k];
    if (!id) return el('i');
    const img = el('img', { src: `https://i.ytimg.com/vi/${id}/mqdefault.jpg`, alt: '', loading: 'lazy' });
    img.onerror = () => img.remove();
    return el('i', {}, img);
  }));
}
const plById = (id) => last?.playlists?.find((p) => p.id === id);
function plRefresh() {
  if (!last) return;
  renderPlHead(last);
  if (!$('#playlist-menu').hidden) buildPlaylistMenu(last);
  renderDraft();
}
function plReport(kind, text, act = null) {
  plNote = { kind, text, act };
  moodReport(kind === 'working' ? 'info' : kind, text); // under the dropdowns too, for when the menu is closed
}

async function pickPlaylist(p) {
  closeMenus();
  await act('/api/playlist', 'POST', { id: p ? p.id : null });
  // Picking re-reads it when the last sync is over a day old (design 07).
  if (p?.stale) syncPlaylist(p.id);
}

async function syncPlaylist(id) {
  if (plSyncing.has(id)) return;
  plSyncing.add(id);
  plMenu = {};
  plRefresh();
  const name = plById(id)?.name ?? 'Playlist';
  try {
    const r = await api(`/api/playlists/${id}/sync`, 'POST');
    plNew[id] = new Set(r.new_ids);
    plSynced = { id, kind: 'ok', text: syncNote(r) };
  } catch (e) { plSynced = { id, kind: 'failed', text: `${name}: ${e.message}` }; }
  plSyncing.delete(id);
  if (last?.playlist?.id !== id) moodReport(plSynced.kind, plSynced.text); // not on screen in the panel
  draft.for = null; // reload the rows
  try { setState(await api('/api/state')); } catch (e) { report(e); }
  plRefresh();
}

async function savePlaylist(url, genre) {
  if (!url) return;
  plAdd = null;
  plSaving++;
  plNote = null;
  plRefresh();
  try {
    const r = await api('/api/playlists', 'POST', { url, genre: genre || null });
    if (r.existing) {
      // Saving it twice opens the saved one (design 12).
      plReport('info', `${r.playlist.name} is already saved. It was synced ${ago(r.playlist.synced_at)}.`,
        [`Play from ${r.playlist.name}`, 'play', () => pickPlaylist(r.playlist)]);
    } else plReport('working', savedNote(r));
    plSaving--;
    setState(await api('/api/state'));
  } catch (e) {
    plSaving--;
    // Errors stay in the Add field, next to the link, with the one action that fixes it (design 10-11).
    plAdd = { url, genre, err: e.message, act: /[?&]v=|youtu\.be\//.test(url) ? ['Add this song', 'plus', () => addSong(url)] : null };
  }
  plRefresh();
}
async function addSong(url) {
  plAdd = null;
  try {
    const r = await api('/api/add', 'POST', { url });
    plReport('ok', `Added ${songs(r.added)} to the library.`);
    setState(await api('/api/state'));
  } catch (e) { plAdd = { url, err: e.message }; }
  plRefresh();
}

async function patchPlaylist(id, body) {
  try {
    await api(`/api/playlists/${id}`, 'PATCH', body);
    draft.for = null; // before setState: the reload it starts must not be dropped as stale
    setState(await api('/api/state'));
  } catch (e) { report(e); }
  plRefresh();
}
async function removePlaylist(p) {
  plMenu = {};
  closeMenus();
  try {
    const r = await api(`/api/playlists/${p.id}`, 'DELETE');
    setState(r.state);
    // No confirm step: the songs stay (design 08/09).
    plNote = null;
    moodReport('info', removedNote(r));
  } catch (e) { report(e); }
}

function startRename(p) {
  renaming = p.id;
  plMenu = {};
  plRefresh();
  document.querySelector('.pl-rename')?.select();
}
function renameInput(p) {
  const done = (save) => {
    if (renaming !== p.id) return;
    const v = inp.value.trim();
    renaming = null;
    if (save && v && v !== p.name) patchPlaylist(p.id, { name: v }); else plRefresh();
  };
  const inp = el('input', { className: 'pl-rename', value: p.name, 'aria-label': `Rename ${p.name}`, maxLength: 100,
    onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); done(true); } if (e.key === 'Escape') { e.stopPropagation(); done(false); } },
    onblur: () => done(true) });
  return inp;
}

/** The ⋯ menu (design 08), in the dropdown or under the playlist's head. */
function manageMenu(p) {
  const item = (ic, label, desc, onclick, cls = '') => el('button', { type: 'button', className: `mi ${cls}`.trim(), onclick },
    icon(ic), el('span', { className: 'ot' }, el('b', { textContent: label }), desc ? el('small', { textContent: desc }) : null));
  if (plMenu.sub === 'genre') {
    return el('div', { className: 'pl-manage', role: 'group', 'aria-label': `Treat ${p.name} songs as` },
      ...[null, ...(last?.genres ?? [])].map((g) => option(g ?? 'Their own genres', g ? '' : 'Each song keeps its own genre',
        (p.genre ?? null) === g, () => { plMenu = {}; patchPlaylist(p.id, { genre: g }); })));
  }
  return el('div', { className: 'pl-manage', role: 'group', 'aria-label': `${p.name}: more` },
    item('pencil', 'Rename', 'Default is the name on YouTube', () => startRename(p)),
    item('tag', 'Treat songs as…', p.genre ? `All as ${p.genre}` : 'A genre for the whole list, or their own genres',
      () => { plMenu = { ...plMenu, sub: 'genre' }; plRefresh(); }),
    item('refresh-cw', 'Sync now', `Last synced ${ago(p.synced_at)}`, () => syncPlaylist(p.id)),
    el('a', { className: 'mi', href: p.url, target: '_blank', rel: 'noopener', onclick: () => { plMenu = {}; closeMenus(); } },
      icon('external-link'), el('span', { className: 'ot' }, el('b', { textContent: 'Open on YouTube' }))),
    item('trash-2', 'Remove playlist', 'Songs stay in your library', () => removePlaylist(p), 'danger'));
}

function addField() {
  const a = plAdd;
  const input = el('input', { id: 'pl-url', type: 'url', value: a.url ?? '', placeholder: 'youtube.com/playlist?list=…', autocomplete: 'off',
    spellcheck: false, 'aria-describedby': 'pl-err', 'aria-label': 'YouTube playlist link' });
  const save = el('button', { type: 'submit', className: 'btn primary needs-server', disabled: !!a.err }, icon('bookmark'), 'Save');
  input.oninput = () => { a.url = input.value; if (a.err) { a.err = null; a.act = null; save.disabled = false; field.classList.remove('bad'); err.replaceChildren(); act.replaceChildren(); } };
  const field = el('label', { className: `field${a.err ? ' bad' : ''}` }, icon('link'), input);
  const genre = el('select', { id: 'pl-genre', 'aria-label': 'Treat songs as', onchange: () => { a.genre = genre.value; } },
    el('option', { value: '', textContent: 'Their own genres' }), ...(last?.genres ?? []).map((g) => el('option', { value: g, textContent: g })));
  genre.value = a.genre ?? '';
  const err = el('p', { id: 'pl-err', className: 'field-msg bad', role: 'alert' }, ...(a.err ? [icon('circle-alert'), a.err] : []));
  const act = el('span', {}, a.act ? el('button', { type: 'button', className: 'btn', onclick: a.act[2] }, icon(a.act[1]), a.act[0]) : null);
  const form = el('form', { className: 'pl-add', onsubmit: (e) => { e.preventDefault(); savePlaylist(input.value.trim(), genre.value); } },
    el('p', { className: 'label', textContent: 'Add a YouTube playlist' }),
    el('div', { className: 'row-fields' }, field, save), err, act,
    el('label', { className: 'inline-label' }, 'Treat songs as', genre));
  form.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); plAdd = null; plRefresh(); $('#src-playlist').focus(); } });
  return form;
}

function noteEl(n) {
  if (!n) return null;
  const ic = { working: 'loader', ok: 'circle-check', failed: 'circle-alert', warn: 'circle-alert', info: n.act ? 'bookmark' : 'info' }[n.kind];
  return el('div', { className: 'pl-note' },
    el('p', { className: `report ${n.kind}`, role: 'status' }, icon(ic, n.kind === 'working' ? 'spin' : ''), el('span', { textContent: n.text })),
    n.act ? el('button', { type: 'button', className: 'btn', onclick: n.act[2] }, icon(n.act[1]), n.act[0]) : null);
}

function buildPlaylistMenu(s) {
  const pls = s.playlists ?? [], cur = s.playlist?.id ?? null;
  const drop = plMenu.where === 'drop' ? plById(plMenu.id) : null;
  const rows = pls.map((p) => {
    const syncing = plSyncing.has(p.id);
    const o = option(p.name, syncing ? 'Syncing with YouTube…' : plCount(p), p.id === cur, () => pickPlaylist(p), [
      iconBtn(syncing ? 'loader' : 'refresh-cw', `Sync ${p.name} with YouTube`, () => syncPlaylist(p.id), `hov${syncing ? ' busy' : ''}`),
      iconBtn('ellipsis', `More for ${p.name}`, () => { plMenu = plMenu.id === p.id ? {} : { id: p.id, where: 'drop' }; buildPlaylistMenu(last); }, 'hov')],
    cover(p.covers));
    o.classList.add('pl-opt');
    if (renaming === p.id) o.querySelector('.ot b').replaceWith(renameInput(p));
    return [o, drop?.id === p.id ? manageMenu(p) : null];
  });
  $('#playlist-menu').replaceChildren(...[
    option('None', 'Vibe or party decides', !cur, () => pickPlaylist(null)),
    pls.length || plSaving ? el('p', { className: 'label grp', textContent: 'Saved' }) : null,
    ...Array.from({ length: plSaving }, () => el('div', { className: 'opt building pl-opt', role: 'status' },
      el('span', { className: 'cover reading' }, icon('loader', 'spin')),
      el('span', { className: 'ot' }, el('b', { textContent: 'YouTube playlist' }), el('small', {}, icon('loader', 'spin'), ' Reading playlist…')))),
    ...rows.flat(),
    plAdd ? addField() : el('button', { type: 'button', className: 'opt-add', onclick: () => { plAdd = { url: '', genre: '' }; plNote = null; buildPlaylistMenu(last); $('#pl-url')?.focus(); } },
      icon('plus'), 'Add a YouTube playlist'),
    noteEl(plNote)].filter(Boolean));
  if (renaming) document.querySelector('#playlist-menu .pl-rename')?.focus();
}

/** Phone Vibe tab: the saved playlists as chips (picking only; saving lives on the desktop). */
function renderPlChips(s) {
  const pls = s.playlists ?? [];
  $('#pl-chips-h').hidden = !pls.length;
  $('#pl-chips').replaceChildren(...(pls.length ? [null, ...pls] : []).map((p) => el('button', {
    type: 'button', className: 'chip', textContent: p ? p.name : 'None', 'aria-pressed': String((s.playlist?.id ?? null) === (p?.id ?? null)),
    onclick: () => pickPlaylist(p) })));
}

/** The playlist's head over its rows (design 04-08): cover, name, counts, ↻ ⋯, order, at the end. */
function renderPlHead(s) {
  const p = s.source?.kind === 'playlist' ? s.playlist : null, head = $('#pl-head');
  head.hidden = !p;
  if (!p) { head.replaceChildren(); return; }
  if (head.contains(document.activeElement) && document.activeElement.matches('input')) return; // renaming
  const syncing = plSyncing.has(p.id), open = plMenu.where === 'head' && plMenu.id === p.id;
  const more = iconBtn('ellipsis', `More for ${p.name}`, () => { plMenu = open ? {} : { id: p.id, where: 'head' }; plAtEnd = false; renderPlHead(last); }, `framed${open ? ' on' : ''}`);
  more.setAttribute('aria-expanded', String(open));
  const end = AT_END.find((x) => x[0] === p.at_end) ?? AT_END[0];
  const endBtn = el('button', { type: 'button', className: 'btn raised small-btn', 'aria-expanded': String(plAtEnd),
    onclick: () => { plAtEnd = !plAtEnd; plMenu = {}; renderPlHead(last); } }, end[1], icon('chevron-down', 'caret'));
  head.replaceChildren(...[
    el('div', { className: 'pl-top' }, cover(p.covers, 'big'),
      el('div', { className: 'pl-name' }, el('p', { className: 'label', textContent: 'Playlist' }),
        renaming === p.id ? renameInput(p) : el('h3', { textContent: p.name }),
        el('p', { className: `pl-meta${syncing ? ' working' : ''}`, textContent: syncing ? 'Syncing with YouTube…' : plMeta(p) })),
      iconBtn(syncing ? 'loader' : 'refresh-cw', `Sync ${p.name} with YouTube`, () => syncPlaylist(p.id), `framed${syncing ? ' busy' : ''}`),
      more),
    open ? manageMenu(p) : null,
    el('div', { className: 'pl-ctl' },
      el('div', { className: 'segmented small', role: 'group', 'aria-label': 'Order' },
        ...[['playlist', 'Playlist order'], ['mix', 'Mix order']].map(([k, label]) => el('button', { type: 'button', textContent: label,
          'aria-pressed': String(p.order === k), onclick: () => p.order !== k && patchPlaylist(p.id, { order: k }) }))),
      el('span', { className: 'at-end' }, el('span', { className: 'dim small', textContent: 'At the end' }), endBtn)),
    plAtEnd ? el('div', { className: 'pl-manage at-end-list', role: 'group', 'aria-label': 'At the end' },
      ...AT_END.map(([k, label, desc]) => option(label, desc, p.at_end === k, () => { plAtEnd = false; patchPlaylist(p.id, { at_end: k }); }))) : null].filter(Boolean));
  if (renaming === p.id) head.querySelector('.pl-rename')?.focus();
}

function plRow(r) {
  const { x } = r, p = draft.playlist, out = r.state === 'excluded';
  const isNew = !!plNew[p?.id]?.has(x.id) && !['played', 'playing', 'next', 'kept'].includes(x.state);
  const keep = async () => {
    r.working = true; renderDraft();
    try { setState(await api('/api/suggestions/accept', 'POST', { id: x.id, artist: x.artist, title: x.title })); x.state = 'kept'; }
    catch (e) { report(e); }
    r.working = false; renderDraft();
  };
  const body = { id: x.id };
  const exclude = async () => {
    try { await api(`/api/playlists/${p.id}/exclude`, 'POST', body); } catch (e) { report(e); return; }
    r.state = 'excluded'; r.until = Date.now() + UNDO_MS; r.at = draft.rows.indexOf(r);
    renderDraft();
    clearTimeout(sweepTimer);
    sweepTimer = setTimeout(sweep, UNDO_MS + 50);
    try { setState(await api('/api/state')); } catch { /* next poll */ }
  };
  const undo = async () => {
    try { await api(`/api/playlists/${p.id}/unexclude`, 'POST', body); } catch (e) { report(e); return; }
    r.state = 'new'; draft.for = null; setState(await api('/api/state').catch(() => last));
  };
  const xBtn = () => el('button', { type: 'button', className: 'icon-btn excl', title: 'Skip it in this playlist from now on',
    'aria-label': `Skip ${x.title} in ${p?.name ?? 'this playlist'}`, onclick: exclude }, icon('x'));
  const st = x.state;
  const acts = out ? el('button', { type: 'button', className: 'btn undo', onclick: undo }, icon('undo-2'), 'Undo')
    : ['played', 'playing', 'failed'].includes(st) ? null
      : el('span', { className: 'acts' },
        r.working ? el('span', { className: 'badge working' }, icon('loader', 'spin'), 'Adding…')
          : st === 'next' || (st === 'waiting' && !isNew) ? null
            : el('button', { type: 'button', className: `icon-btn keep${st === 'kept' ? ' on' : ''}`, disabled: st === 'kept',
              'aria-label': st === 'kept' ? `${x.title} is in Up next` : `Keep ${x.title}: add it to Up next`,
              title: st === 'kept' ? 'In Up next' : 'Add to Up next', onclick: keep }, icon('check')),
        st === 'kept' || r.working ? null : xBtn());
  return el('li', { className: `row draft-row pl-row ${st}${isNew ? ' new' : ''}${out ? ' excluded' : ''}` }, thumb(x),
    el('span', { className: 'text' }, el('span', { className: 't', textContent: x.title }),
      el('span', { className: 's', textContent: out ? 'Skipped in this playlist from now on' : plRowNote(x, hooks.decks, isNew) })),
    el('span', { className: 'meta', textContent: x.bpm ? x.bpm.toFixed(1) : '–', title: x.bpm ? 'BPM' : 'Tempo unknown yet' }), acts);
}

function renderPlaylistDraft() {
  const p = last?.playlist ?? draft.playlist;
  $('#sugg-all').hidden = true;
  $('#sugg-done').hidden = true;
  const msg = $('#sugg-msg');
  msg.hidden = !(draft.loading || draft.error);
  msg.textContent = draft.loading ? 'Reading the playlist…' : draft.error ?? '';
  $('#tab-sugg .n').textContent = String(draft.rows.filter((r) => r.state !== 'excluded').length);
  $('#sugg').replaceChildren(...draft.rows.map(plRow));
  // Songs YouTube won't serve: listed once per new failure, then skipped quietly (design 12).
  if (p?.failed > plFailTold(p.id)) { plFailShown[p.id] = p.failed; localStorage.setItem(`autodeck.plfail.${p.id}`, String(p.failed)); }
  const notes = [
    last?.notice ? noteEl({ kind: 'warn', text: last.notice }) : null,
    plSynced?.id === p?.id ? noteEl(plSynced) : null,
    p && plFailShown[p.id] ? noteEl({ kind: 'warn', text: failedNote({ ...p, failed: plFailShown[p.id] }) }) : null].filter(Boolean);
  $('#pl-notes').replaceChildren(...notes);
}

// --- search -----------------------------------------------------------------
// Case- and diacritic-insensitive, so "sarba" finds "Sârba".
const fold = (t) => (t || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase();
const mmss = (s) => (s ? `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}` : '');
const localMatches = (q, n = 10) => {
  const words = fold(q).split(/\s+/).filter(Boolean);
  return (last?.pool ?? []).filter((s) => words.every((w) => fold(`${s.title} ${s.channel}`).includes(w))).slice(0, n);
};

function resultRow(r) {
  return el('li', { className: 'row res-row' }, thumb(r),
    el('span', { className: 'text' }, el('span', { className: 't', textContent: r.title }),
      el('span', { className: 's' }, r.status ? badge(r.status) : null,
        [r.channel, mmss(r.duration)].filter(Boolean).join(' · '))),
    r.id === last?.playing ? null : deckBtn(r, 'search'));
}

function showLocal(q) {
  const local = localMatches(q);
  $('#res-lib').replaceChildren(...local.map((s) => resultRow(s)));
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
    $('#res-yt').replaceChildren(...yt.map((r) => resultRow(r)));
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
        : [...s.queue, ...ready.filter((p) => !s.queue.some((q) => q.id === p.id))].slice(0, 12);
  const why = (p) => (opTab === 'loved' && !q ? 'You loved what came after it' : [p.genre, p.bpm ? `${p.bpm.toFixed(1)} BPM` : null,
    p.camelot, p.danceability != null ? `${Math.round(p.danceability * 100)}% danceable` : null].filter(Boolean).join(' · '));
  $('#op-list').replaceChildren(...list.map((p) => el('li', { className: 'row op-row' },
    el('span', { className: 'text' }, el('span', { className: 't', textContent: p.title }), el('span', { className: 's', textContent: why(p) })),
    p.id === openerId ? el('span', { className: 'on-play' }, icon('play'), 'On PLAY')
      : el('button', { type: 'button', className: 'btn', 'aria-label': `Load ${p.title} in A`, onclick: () => hooks.loadOpener(p) },
        icon('arrow-down-to-line'), 'Load in A'))));
  if (!list.length) $('#op-list').append(el('li', { className: 'empty-note', textContent: q ? 'No ready song matches.' : 'Nothing here yet.' }));
  $('#op-foot span').textContent = `Up next is planned from whatever plays first: ${s.queue.length} ready in ${srcName(s)}.`;
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
const BROWSER_NAME = { chrome: 'Chrome', safari: 'Safari', firefox: 'Firefox', brave: 'Brave', edge: 'Edge' };
/** What Settings says under "YouTube login from", from GET /api/settings' `youtube`. */
export function browserNote(yt) {
  if (yt?.from_env) return `Set to "${yt.browser}" by the AUTODECK_BROWSER environment variable, which wins over this choice`;
  if (!yt || yt.browser === 'none') return 'No login: public videos and playlists work, Liked Music does not';
  const name = BROWSER_NAME[yt.browser] ?? yt.browser;
  // Safari keeps its cookies in a protected folder; the other browsers' are behind a Keychain prompt.
  if (yt.browser === 'safari') return 'Uses your Safari login. autodeck needs Full Disk Access (System Settings → Privacy & Security) to read it';
  return `Uses your ${name} login. macOS asks once for Keychain access: click Always Allow`;
}
/** The Installation line in Settings, from GET /api/settings' `runtime`. */
export function runtimeNote(rt) {
  if (!rt?.standalone) return { text: `Running from the repo (Python ${rt?.python ?? '?'})`, repair: false };
  const gb = (rt.size / 1e9).toFixed(1);
  return { text: `autodeck ${rt.app_version ?? '?'} · Python ${rt.python} · ${gb} GB installed`, repair: true };
}
async function repairInstall() {
  if (!confirm('Repair reinstalls autodeck\'s packages and re-checks its models (a large download), then restarts the app. Continue?')) return;
  try { await api('/api/setup/repair', 'POST'); $('#rt-state').textContent = 'Restarting to repair…'; }
  catch (e) { $('#rt-err').textContent = e.message; }
}
function showSettings(st) {
  const k = lastfmState(st.lastfm);
  const el = $('#lastfm-state');
  el.className = `key-state ${k.cls}`;
  el.replaceChildren(icon(k.cls === 'set' ? 'circle-check' : 'info'), k.text);
  $('#lastfm-remove').hidden = !k.removable;
  if (st.library) $('#lib-path').textContent = st.library;
  if (st.runtime) {
    const r = runtimeNote(st.runtime);
    $('#rt-state').replaceChildren(icon('info'), r.text);
    $('#rt-repair').hidden = !r.repair;
  }
  if (st.youtube) {
    $('#yt-browser').value = st.youtube.browser;
    $('#yt-browser').disabled = st.youtube.from_env; // the env var wins; changing it here would do nothing
    $('#yt-state').replaceChildren(icon('info'), browserNote(st.youtube));
  }
}
async function saveBrowser(browser) {
  $('#yt-err').textContent = '';
  try { showSettings(await api('/api/settings/youtube', 'PUT', { browser })); } catch (e) { $('#yt-err').textContent = e.message; loadSettings(); }
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
    draft.for = null; // suggestions may work now (or stop): reload them on the next state
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
  // Sources: Vibe / Party dropdowns (one open at a time; Esc or a click outside closes).
  for (const n of MENUS) {
    $(`#src-${n}`).addEventListener('click', () => toggleMenu(n));
    $(`#${n}-menu`).addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeMenus(); $(`#src-${n}`).focus(); } });
  }
  $('#sugg-all').addEventListener('click', addAll);
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
  const tabs = TABS;
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
    if (!e.target.closest('.src-wrap')) closeMenus();
    // The playlist head's ⋯ and "At the end" lists close on a click elsewhere.
    if ((plMenu.where === 'head' || plAtEnd) && !e.target.closest('#pl-head')) { plMenu = {}; plAtEnd = false; if (last) renderPlHead(last); }
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
  sortable($('#queue'), {
    onStart: () => { dragging = true; },
    onEnd: () => { dragging = false; },
    onMove: (gap, index) => { gap.textContent = `Drop here: plays ${ordinal(index + 1)}`; },
    onDrop: (id, index) => {
      if (!last) return;
      const ids = last.queue.map((x) => x.id);
      if (ids.indexOf(id) === index) { flush(); return; } // dropped where it was
      reorder(ids, id, index);
    },
  });
  // Phone: long-press a row (Up next, Played, Library) for its action sheet. Touch/pen only, and
  // not on the drag grip (that's sortable's). The click that follows the press is swallowed.
  let press = 0, pressAt = null, pressed = false;
  const unpress = () => { clearTimeout(press); pressAt = null; };
  for (const id of ['#queue', '#history', '#pool']) {
    const list = $(id);
    list.addEventListener('pointerdown', (e) => {
      pressed = false; // a long-press often ends without a click: never swallow the next tap
      const li = e.target.closest('li');
      if (e.pointerType === 'mouse' || !li?.pick || e.target.closest('.grip, button, select')) return;
      pressAt = [e.clientX, e.clientY];
      press = setTimeout(() => { pressed = true; unpress(); openSheet(li.pick); }, 500);
    });
    list.addEventListener('pointermove', (e) => { if (pressAt && Math.hypot(e.clientX - pressAt[0], e.clientY - pressAt[1]) > 8) unpress(); });
    list.addEventListener('pointerup', unpress);
    list.addEventListener('pointercancel', unpress);
    list.addEventListener('click', (e) => { if (pressed) { pressed = false; e.preventDefault(); e.stopPropagation(); } }, true);
    list.addEventListener('contextmenu', (e) => { if (e.target.closest('li')?.pick && matchMedia('(max-width: 600px)').matches) e.preventDefault(); });
  }
  $('#sheet').addEventListener('click', (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); }); // the backdrop
  // A select closing (or losing focus) releases a deferred render.
  document.addEventListener('focusout', (e) => { if (e.target.tagName === 'SELECT') setTimeout(flush); });

  // Add by link: the popover, or drop / paste a link anywhere on the page.
  $('#add-btn').addEventListener('click', () => togglePop('add'));
  $('#settings-btn').addEventListener('click', () => togglePop('settings'));
  $('#menu-btn').addEventListener('click', () => togglePop('menu'));
  $('#menu-add').addEventListener('click', () => togglePop('add', true));
  $('#menu-settings').addEventListener('click', () => togglePop('settings', true));
  $('#yt-browser').addEventListener('change', (e) => saveBrowser(e.target.value));
  $('#rt-repair').addEventListener('click', repairInstall);
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
