// Mixer state machine: which deck is on air, the scheduled transition, replanning,
// Mix now and the manual crossfader, plus what shows it: the ON AIR sign and the lanes.
// Timing math lives in schedule.js (pure, tested), lane geometry in lanes.js (pure, tested),
// Web Audio plumbing in deck.js, everything else on the page in ui.js.
import * as S from './schedule.js';
import { Deck, loadTrack, laneText } from './deck.js';
import { Lane, laneLeft, mixProgress, mixTag, mixBars, incomingStart, isBeatmatched, mixesWith } from './lanes.js';
import { api, initUi, setState, getState, report, FX_LABEL, FX_ICON, icon, setOpener, toTab, setPick, pairOf, isLoved, autoLabel, fxOptionLabel, withThumbs, fxShape } from './ui.js';
import { initOnboard, renderOnboard } from './onboard.js';
import { initMcp, renderMcp } from './mcp.js';

const $ = (sel) => document.querySelector(sel);
const POLL_MS = 2000;
const MAX_RETRIES = 3; // tooSoon refetches before giving up and letting the song play out
const RATE_MS = 30000; // "How was that mix?" stays this long after a mix, then goes by itself

const M = {
  ctx: null, decks: [], cur: 0,
  plan: null,      // scheduled transition out of decks[cur] (schedule.plan + res)
  next: null,      // latest fetched transition, waiting to be scheduled
  // What the last transition request was for: replans reuse deck/after, and compare
  // version/inId to the polled state (schedule.shouldReplan).
  req: null,       // { seq, deck, id, after, version, inId, retries, loading }
  seq: 0,          // bumped per request; a response with an old seq is dropped (latest wins)
  timers: [],      // setTimeouts belonging to M.plan
  manual: false,
  force: null,     // owner's forced effect for the current song's exit (null = app's pick)
  fxPlan: null,    // the plan the effect picker was last built for
  rate: null,      // { from, to, title, until } the hand-over the sign asks about
  on: false,       // live mode (see live() / setLive())
  lastPlan: null,  // the plan that just finished: keeps the lane view steady through the swap
};
// Reload resumes where you were: the playing song + position, saved about once a second.
// localStorage, not the server: it's this browser's playback, and it must survive a reload
// even while the server restarts. Older than RESUME_MAX_MS = a new session, start fresh.
const RESUME_KEY = 'autodeck.resume';
const RESUME_MAX_MS = 12 * 3600 * 1000;
let lastSave = 0;
function savedResume() {
  try {
    const r = JSON.parse(localStorage.getItem(RESUME_KEY));
    return r && Date.now() - r.at < RESUME_MAX_MS ? r : null;
  } catch { return null; }
}
function saveResume(t) {
  if (Date.now() - lastSave < 1000) return;
  const d = M.decks[M.cur];
  if (!d?.info || !d.started(t)) return;
  lastSave = Date.now();
  localStorage.setItem(RESUME_KEY, JSON.stringify({ id: d.info.id, title: d.info.title, channel: d.info.channel,
    pos: d.pos(t), at: lastSave }));
}

const now = () => M.ctx.currentTime;
// Status line in the sign (a polite live region). Transient messages clear themselves so a
// stale "Mix already under way." doesn't hang around; sticky ones wait for the next status.
let statusTimer = 0;
const status = (t, sticky = false) => {
  clearTimeout(statusTimer);
  $('#status').textContent = t;
  if (t && !sticky) statusTimer = setTimeout(() => { $('#status').textContent = ''; }, 8000);
};
const at = (ctxTime, fn) => M.timers.push(setTimeout(fn, Math.max(0, ctxTime - now()) * 1000));
const clearTimers = () => { M.timers.forEach(clearTimeout); M.timers = []; };
const curId = () => M.decks[M.cur].info?.id;
// Live = the mixer owns the screen (from the first song starting until it stops for good).
// One explicit flag, not "is a deck busy": during an auto-restart gap no deck is busy, and the
// idle UI (opener, lane note) must not flash in and leave stale state behind.
const live = () => M.on;

// --- transitions ------------------------------------------------------------
/** Ask the server for the transition out of `deck`'s song. `after` = rendered seconds. */
async function requestPlan(deck, after = null, retries = 0) {
  const d = M.decks[deck], id = d.info.id, seq = ++M.seq;
  M.req = { seq, deck, id, after, version: getState()?.version, mode: !!getState()?.beat_to_beat,
            inId: undefined, retries, loading: true };
  status('Preparing next mix…', true);
  let res;
  try {
    const q = new URLSearchParams({ from: id });
    if (after != null) q.set('after', after.toFixed(3));
    if (M.force) q.set('kind', M.force);
    res = await api(`/api/transition?${q}`);
    if (res.transition) res.track = await loadTrack(M.ctx, res.in_deck); // decode before scheduling
  } catch (e) {
    if (seq === M.seq) {
      M.req.loading = false; M.req.inId = null;
      // Server restarting / network blip: keep trying while this song still plays, so a
      // short outage never ends the mix. songEnded() is the last safety net.
      status(`Next mix failed (${e.message}) — retrying…`, true);
      setTimeout(() => { if (seq === M.seq && M.decks[deck]?.info?.id === id && !M.plan) requestPlan(deck, after, retries); }, 3000);
    }
    return;
  }
  if (seq !== M.seq) return; // a newer request superseded this one
  M.req.loading = false;
  M.req.inId = res.transition?.in_id ?? null;
  M.next = { ...res, deck, id };
  tryApply();
}

/** Schedule M.next if its outgoing deck is on air and the other slot is free. */
function tryApply() {
  const n = M.next;
  if (!n || M.manual || n.deck !== M.cur || n.id !== curId()) return;
  if (!n.transition) {
    M.next = null;
    // Latest response wins: no next song any more (e.g. it was deleted) drops the old plan.
    if (M.plan && S.canReplan(now(), M.plan.T0)) cancelPlan();
    if (!M.plan) status('');
    return;
  }
  const t = now(), out = M.decks[M.cur], inc = M.decks[1 - M.cur];
  // Replan guard: a plan whose blend is (about to be) audible is never replaced.
  if (M.plan && !S.canReplan(t, M.plan.T0)) { M.next = null; return; }
  if (M.plan) cancelPlan();
  if (inc.busy) return; // previous transition's tail still on that slot; retried at swap
  // The outgoing leaves at its settled tempo (bpm_playing), so that's the echo's beat.
  const beat = 60 / (out.info.bpm_playing || 120);
  const p = S.plan(n, out.zero, t, beat);
  if (S.tooSoon(p.T0, t)) {
    M.next = null;
    // T0 passed while rendering (or a pulled-back song's exit is behind us): ask for
    // the next boundary instead. Capped so a song at its very end can't loop forever.
    if (M.req.retries < MAX_RETRIES) requestPlan(M.cur, out.pos(t) + S.PREP_LEAD, M.req.retries + 1);
    else status('No usable mix point left — this song plays to its end.', true);
    return;
  }
  M.next = null;
  M.plan = { ...p, res: n, inDeck: 1 - M.cur, bar: 4 * beat };
  status('');
  const inIdx = 1 - M.cur;
  inc.start(n.track, p.inStart, p.inOffset, songEnded);
  inc.planId = n.plan_id; // reported with /api/playing so the server commits this render's map
  out.apply(p.out, t);
  inc.apply(p.in, t);
  // Outgoing stops in ctx time at the end of the transition (exact for cut/dropswap; gain
  // is already 0 for the others). The swap timer below only moves the UI roles.
  out.stopAt(p.end);
  if (p.kind === 'blend' || p.kind === 'half_double') {
    const drift = S.driftMs(out.info.downbeats_rendered, n.in_deck.downbeats_rendered, out.zero, p.inZero, p.T0, p.end);
    console.log(`[autodeck] ${p.kind} drift ${drift?.toFixed(2) ?? 'n/a'} ms (want < 10)`);
  }
  at(p.inStart, () => onStarted(inIdx));
  at(p.end, swap);
}

function cancelPlan() {
  clearTimers();
  M.decks[M.plan.inDeck].stop();
  const t = now(), out = M.decks[1 - M.plan.inDeck];
  out.unstop();
  // Before T0: undo the scheduled fade-out. After T0 (grab mid echo-out): keep the
  // current gain; takeOver's hold() freezes it there.
  const reset = S.outgoingOnCancel(t, M.plan.T0);
  if (reset) out.apply(reset, t);
  M.plan = null;
}

/** A deck began sounding: tell the server, then prepare its exit right away (renders take seconds). */
async function onStarted(deck) {
  const d = M.decks[deck];
  try { setState(await api('/api/playing', 'POST', { id: d.info.id, plan_id: d.planId })); } catch (e) { status(e.message); }
  requestPlan(deck);
}

/** Transition finished: incoming becomes current, old outgoing source is released. */
function swap() {
  const old = M.cur;
  M.timers = [];
  // The sign asks once about every hand-over (design xWVJy): a 🔥 counts again even for a pair
  // that's loved already, so it asks even then. It goes by itself after RATE_MS.
  const a = M.decks[old].info, b = M.decks[1 - old].info, kind = M.plan?.kind;
  if (a && b) {
    M.rate = { from: a.id, to: b.id, effect: kind, until: Date.now() + RATE_MS,
      title: [`${a.title} → ${b.title}`, FX_LABEL[kind]].filter(Boolean).join(' · ') };
  }
  M.lastPlan = M.plan;
  M.plan = null;
  // The old source stops itself (stopAt in tryApply); its onended frees the slot.
  M.decks[old].param('echo').setValueAtTime(0, now());
  M.cur = 1 - old;
  if (M.req?.loading) status('Preparing next mix…', true); // the sign shows it until the plan arrives
  tryApply(); // the new current's exit may already be fetched
}

function songEnded(deck) {
  if (M.decks.indexOf(deck) === M.cur && !M.plan) {
    // No mix was scheduled (server was down, or the plan never arrived): don't go silent,
    // start the next queued song straight away. Only a truly empty queue stops the music.
    const next = getState()?.queue.find((q) => q.id !== deck.info?.id);
    if (next) {
      restart(next);
      return;
    }
    status('Stopped — end of queue. Add songs and press Play to start again.', true);
    stopLive();
  }
  tryApply(); // a slot just freed: a waiting plan may now be scheduled
}

/** Auto-restart after a missed mix. The server may be restarting (that's often why the mix was
 * missed), so retry a few times; if it still fails the music has stopped: back to the PLAY
 * lamp, never a silent page that still says ON AIR. */
const RESTART_TRIES = 3;
function restart(song, tries = 1) {
  startSong(song).catch((e) => {
    if (tries < RESTART_TRIES) {
      status(`Couldn't start ${song.title} (${e.message}) — retrying…`, true);
      setTimeout(() => { if (live() && !M.decks.some((d) => d.busy)) restart(song, tries + 1); }, 3000);
      return;
    }
    status(`Stopped: couldn't start ${song.title} (${e.message}). Press Play to try again.`, true);
    stopLive();
  });
}

/** The music stopped for good: idle UI (PLAY lamp, opener) again. */
function stopLive() {
  setLive(false);
  const s = getState();
  if (s) pickOpener(s);
}

// --- poll + replan ------------------------------------------------------------
function onState(s) {
  // Server-held controls (set from the picker, the API or the MCP DJ):
  // a mix-now request and the forced effect for the current song's exit.
  if (M.mixSeq === undefined) M.mixSeq = s.mix_now_seq;
  else if (s.mix_now_seq > M.mixSeq) { M.mixSeq = s.mix_now_seq; if (M.ctx) mixNow(); }
  if ((s.next_effect ?? null) !== M.force) {
    M.force = s.next_effect ?? null;
    M.fxPlan = null;
    if (M.ctx && M.req && !M.req.loading && (!M.plan || S.canReplan(now(), M.plan.T0))) requestPlan(M.cur, M.req.after);
  }
  if (!live()) pickOpener(s);
  fadeOutIfLast(s);
  show('#fx-mcp', s.next_effect_by === 'mcp'); // the assistant's pick, until the owner changes it
  const r = M.req;
  if (!M.ctx || !r || r.loading || M.manual || r.id !== M.decks[r.deck].info?.id) return;
  const nextId = s.queue.find((q) => q.id !== r.id)?.id;
  const T0 = M.plan && M.plan.res.id === r.id ? M.plan.T0 : undefined;
  // After the outgoing blend started (T0 reached) the contract forbids refetching.
  // A beat-to-beat toggle changes the plan for the same next song, so it replans too.
  const modeChanged = !!s.beat_to_beat !== r.mode && S.canReplan(now(), T0);
  if (modeChanged || S.shouldReplan({ stateVersion: s.version, planVersion: r.version, planInId: r.inId, nextId, now: now(), T0 })) {
    requestPlan(r.deck, r.after);
  } else {
    r.version = s.version; // seen; don't re-evaluate until the next change
  }
}

/** "Stop after last" (a playlist's at-the-end choice): its last song fades out over its final
 * seconds; the end of the queue then turns ON AIR off as usual (songEnded -> stopLive). Undone
 * if something joins Up next after all (a kept song, the owner changing the choice). */
const FADE_S = 8;
function fadeOutIfLast(s) {
  const d = M.decks[M.cur];
  if (!M.ctx || !d?.busy || !d.info || M.plan) return;
  const isLast = s.source?.kind === 'playlist' && s.playlist?.at_end === 'stop' && !s.queue.some((q) => q.id !== d.info.id);
  if (isLast === (M.fade === d.info.id)) return;
  const g = d.param('gain'), end = d.zero + d.info.duration_rendered, t = Math.max(now(), end - FADE_S);
  g.cancelScheduledValues(t);
  g.setValueAtTime(1, t);
  if (isLast) g.linearRampToValueAtTime(0, end);
  M.fade = isLast ? d.info.id : null;
}

async function poll() {
  try {
    setState(await api('/api/state'));
    online(true);
  } catch { online(false); /* server restarting; next poll retries */ }
  setTimeout(poll, POLL_MS);
}
/** Server unreachable: the music keeps going from what's loaded; server-only controls dim. */
function online(ok) {
  document.body.classList.toggle('offline', !ok);
  $('#offline').hidden = ok;
}

// --- the opener (before anything plays) ------------------------------------------
// The PLAY lamp holds one song: the one to resume after a reload, else the first in Up next,
// or whatever the owner loaded from "Or start with". Its deck payload is fetched ahead so
// lane A shows the waveform and Play starts without a second /api/deck round trip.
const O = { id: null, title: '', channel: '', at: null, resume: null, chosen: false, info: null };

function pickOpener(s) {
  const ready = (id) => s.pool.some((p) => p.id === id && p.status === 'ready');
  if (O.id && !ready(O.id)) { O.id = null; O.chosen = false; }
  if (!O.id || (!O.chosen && !O.resume)) {
    const r = savedResume();
    if (r && ready(r.id)) loadOpener({ id: r.id, title: r.title, channel: r.channel }, r);
    else if (s.queue[0] && s.queue[0].id !== O.id) loadOpener(s.queue[0]);
  }
  renderIdle();
}

/** Put `song` on the PLAY button. `resume` = the saved position record, if resuming. */
function loadOpener(song, resume = null, chosen = false) {
  Object.assign(O, { id: song.id, title: song.title, channel: song.channel ?? '', resume,
    at: resume ? Math.max(0, resume.pos - 1) : null, chosen, info: null });
  setOpener(song.id);
  laneText($('#deck0'), null);
  $('#deck0 .title').textContent = song.title;
  $('#deck0 .artist').textContent = song.channel ?? '';
  lanes[0].setInfo(null);
  const id = song.id;
  api(`/api/deck/${id}`).then((info) => {
    if (O.id !== id || live()) return;
    O.info = info;
    O.channel = info.channel;
    laneText($('#deck0'), info);
    lanes[0].setInfo(info);
    renderIdle();
  }).catch(() => { /* not ready / server gone: Play fetches it again */ });
  renderIdle();
}

function renderIdle() {
  if (live()) return;
  const s = getState();
  const c = s?.pool_counts ?? {};
  const ready = c.ready ?? 0, coming = (c.queued ?? 0) + (c.downloading ?? 0) + (c.analyzing ?? 0);
  const empty = !s?.pool.length;
  document.body.classList.toggle('empty', empty);
  // Through show(): the live sign reuses these panels, and its cache must see every write.
  for (const sel of ['#side-next', '#side-mix', '#side-rate']) show(sel, false);
  show('.sign-side', true);
  show('#side-idle', !(empty || !O.id));
  show('#side-help', empty || !O.id);
  if (empty) {
    setText('#sign-kicker', 'First launch');
    setText('#sign-title', "Let's get some music in");
    setText('#sign-sub', 'Paste a YouTube link, search, or build a mood.');
    helpButtons([['link', 'Add link', () => openAdd()], ['search', 'Search YouTube', () => focusSearch()],
      ['sparkles', 'Build a mood', () => { toTab('vibe'); $('#genres .build')?.click(); }]]);
    return;
  }
  if (!O.id) {
    setText('#sign-kicker', coming ? `${ready} songs ready · more on the way` : 'Nothing ready');
    setText('#sign-title', coming ? 'Getting songs ready' : 'Nothing to play yet');
    setText('#sign-sub', `${c.analyzing ?? 0} analyzing · ${(c.queued ?? 0) + (c.downloading ?? 0)} queued · ${c.failed ?? 0} failed`);
    helpButtons([['link', 'Add link', () => openAdd()], ['sparkles', 'Build a mood', () => { toTab('vibe'); $('#genres .build')?.click(); }]]);
    return;
  }
  const r = O.resume;
  setText('#sign-kicker', r ? 'Resume where you left off' : coming && ready < 10 ? `${ready} songs ready · more on the way` : 'Ready to start with');
  setText('#sign-title', O.title);
  const stopped = r ? new Date(r.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
  setText('#sign-sub', [O.channel, r ? (O.at != null ? `from ${fmt(O.at)}` : 'from the top') : 'first in Up next',
    r ? `stopped at ${stopped}` : null].filter(Boolean).join('  ·  '));
  const alt = $('#alt-btn');
  alt.innerHTML = '';
  // "From the top" = the song's trimmed start (firstOffset), not literal 0:00 of the render.
  if (r) alt.append(icon(O.at != null ? 'rotate-ccw' : 'history'), O.at != null ? 'Start from the top' : `Resume at ${fmt(r.pos)}`);
  else alt.append(icon('repeat'), 'Pick another opener');
  // Lane A: the opener, loaded; lane B: what follows it, still empty.
  setRole(0, r ? 'loaded · resumes here' : 'loaded · starts at 0:00', 'loaded');
  const startAt = O.at ?? (O.info ? S.firstOffset(O.info) : 0);
  setText('#deck0 .time', O.info ? `${fmt(startAt)} / ${fmt(O.info.duration_rendered)}` : '');
  const nxt = s.queue.find((q) => q.id !== O.id);
  laneText($('#deck1'), null);
  setRole(1, 'empty', '');
  setText('#deck1 .time', '');
  $('#deck1 .title').textContent = nxt?.title ?? '—';
  $('#deck1 .artist').textContent = nxt ? `${nxt.channel} · loads next` : '';
  $('#deck1 .metaline').textContent = nxt ? [nxt.genre, nxt.bpm?.toFixed(1), nxt.camelot].filter(Boolean).join('  ') : '';
  show('#lane-note', !!nxt);
  $('#lane-note span').textContent = nxt ? `Deck B stays empty until A is on air, then ${nxt.title} loads here` : '';
  setText('#xf-tag', 'READY');
}

function helpButtons(items) {
  const box = $('#side-help');
  box.replaceChildren(...items.map(([ic, label, fn], i) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = i ? 'btn' : 'btn primary';
    b.append(icon(ic), label);
    b.onclick = fn;
    return b;
  }));
}

// --- controls ---------------------------------------------------------------
async function play() {
  if (live()) return;
  $('#play').disabled = true;
  try {
    // Autoplay policy: the AudioContext must be created inside the click.
    if (!M.ctx) {
      M.ctx = new AudioContext();
      const master = new GainNode(M.ctx);
      // Live performance FX on the master bus, after both decks, so the owner's hands never
      // fight a scheduled transition's automation (which lives on the deck nodes).
      M.live = liveFx(M.ctx, master);
      M.decks = [0, 1].map((i) => new Deck(M.ctx, master, $(`#deck${i}`)));
    }
    await M.ctx.resume();
    const s = await api('/api/state');
    setState(s);
    // Pressed before the first poll arrived: pick the opener (saved resume point first) now.
    if (!O.id) pickOpener(s);
    // The opener on the PLAY lamp: the song that was playing before the reload (at its
    // position: the server's session still holds its time map, so /api/deck returns the
    // same render), or the first queued song, or one the owner loaded from "Or start with".
    const first = O.id ? { id: O.id, title: O.title } : s.queue[0];
    if (!first) throw new Error('Nothing ready to play yet — add songs first.');
    await startSong(first, O.id ? O.at : null, O.id === first.id ? O.info : null);
  } catch (e) {
    status(e.message, true);
    $('#play').disabled = false;
  }
}

/** Switch between the idle screen (PLAY lamp, opener) and the live one. Both share the sign
 * and lanes, so the write caches are dropped on every switch: nothing written in one mode is
 * trusted in the other (a stale cache once left idle buttons visible during live countdowns). */
function setLive(on) {
  M.on = on;
  document.body.classList.toggle('live', on);
  document.body.classList.toggle('idle', !on);
  textCache.clear();
  shown.clear();
  idleKey = '';
  $('#play').disabled = on;
  for (const el of ['#xfader', '#live-filter', '#live-echo']) $(el).disabled = !on;
  if (on) {
    show('#lane-note', false);
    show('#side-idle', false);
    O.id = null; O.chosen = false; O.resume = null;
  }
}

/** Start `song` from scratch on deck 0 (Play, Resume, or recovering from a missed mix).
 * `at` = rendered seconds to start from (resume), null = the song's own start.
 * `info` = its deck payload if already fetched (the opener preview). */
async function startSong(song, at = null, info = null) {
  status(`Loading ${song.title}…`, true);
  info = info ?? await api(`/api/deck/${song.id}`);
  const track = await loadTrack(M.ctx, info);
  M.decks.forEach((d) => d.stop());
  if (M.plan) cancelPlan();
  M.cur = 0;
  const d = M.decks[0];
  // Every effect control back to idle, not just the gain: a deck that last ran a filter
  // sweep / bass swap / reverb kept its filter closed, and the next Play sounded muffled.
  for (const x of M.decks) x.apply(S.idle(now()), now());
  d.apply([{ param: 'gain', type: 'set', value: 1, time: now() }], now());
  // Resume a little before the saved spot (the save is up to 1 s old); never past the end.
  const offset = at != null ? Math.min(Math.max(at, 0), info.duration_rendered - 5) : S.firstOffset(info);
  d.start(track, now() + 0.1, Math.max(offset, 0), songEnded);
  status('');
  if (!live()) setLive(true); // first Play, or Play after the music stopped
  onStarted(0);
}

function mixNow() {
  if (!M.ctx || !M.decks[M.cur].started(now())) return;
  if (M.plan && now() >= M.plan.T0 - S.REPLAN_MARGIN) { status('Mix already under way.'); return; }
  requestPlan(M.cur, M.decks[M.cur].pos(now()) + S.PREP_LEAD);
}

const fader = $('#xfader');
const faderX = () => fader.valueAsNumber / 1000;

function takeOver() {
  if (!M.ctx || M.manual) return;
  M.manual = true;
  const t = now();
  // An incoming that hasn't started can't be faded in by hand: drop that plan entirely.
  if (M.plan && !M.decks[M.plan.inDeck].started(t)) cancelPlan();
  clearTimers(); // no automatic swap while the owner drives
  M.seq++; // and no in-flight response gets scheduled under the owner's hands
  M.next = null;
  M.decks.forEach((d) => { d.unstop(); d.hold(t); }); // the owner decides who stops now
  status('Manual crossfader — let go and the nearer deck takes over.', true);
}

function drive() {
  if (!M.manual) takeOver();
  if (!M.manual) return;
  const g = S.manualGains(faderX());
  M.decks.forEach((d, i) => d.setGain(g[i], now()));
}

function letGo() {
  if (!M.manual) return;
  const t = now(), x = faderX();
  const started = M.decks.map((d) => d.started(t));
  const { winner, loser } = S.release(x, M.cur, started);
  const w = M.decks[winner], l = M.decks[loser];
  const bar = 240 / (w.info.bpm_playing || 120);
  const ramps = S.releaseRamps(t, bar, S.manualGains(x), winner);
  w.apply(ramps[winner], t);
  if (l.busy) {
    l.apply(ramps[loser], t);
    // ctx-time stop; a re-grab within the bar unstops it (takeOver), and its onended
    // calls tryApply once the slot is free.
    l.stopAt(t + bar);
  }
  const changed = winner !== M.cur;
  M.plan = null;
  M.cur = winner;
  M.manual = false;
  status('');
  fader.value = winner === 0 ? 0 : 1000;
  if (changed || started[loser]) {
    // Tell the server who is on air. Pushed into the incoming: its start timer may not
    // have fired yet (background tabs throttle setTimeout). Pulled back: the incoming was
    // already posted as playing, so hand the current song back.
    api('/api/playing', 'POST', { id: w.info.id, plan_id: w.planId }).then(setState, (e) => status(e.message));
  }
  // Next transition "as usual". Pulled back is an owner override, so the "never refetch
  // after T0" rule doesn't apply (the old plan is gone); its planned exit may be behind
  // us, and tryApply's tooSoon path then asks for the next boundary.
  requestPlan(winner);
}

let pointerHeld = false, keyTimer = 0;
// Pointer capture: pointerup still reaches the fader if the drag ends outside it.
fader.addEventListener('pointerdown', (e) => {
  clearTimeout(keyTimer); // a pending keyboard release must not fire mid-drag
  pointerHeld = true;
  try { fader.setPointerCapture(e.pointerId); } catch { /* pointer already gone */ }
  takeOver();
});
// pointercancel (touch turned into a scroll, OS gesture) and lostpointercapture end a drag
// without a pointerup; missing them left pointerHeld stuck, so manual mode never released
// and auto-mixing stalled. letGo is a no-op once released, so firing twice is harmless.
const pointerRelease = () => { clearTimeout(keyTimer); pointerHeld = false; letGo(); };
for (const ev of ['pointerup', 'pointercancel', 'lostpointercapture']) fader.addEventListener(ev, pointerRelease);
fader.addEventListener('input', () => {
  drive();
  // Keyboard (no pointer held): release after a pause, not on every keyup, so a run of
  // arrow presses is one gesture instead of a snap + refetch per step.
  if (!pointerHeld) { clearTimeout(keyTimer); keyTimer = setTimeout(letGo, S.KEY_RELEASE_MS); }
});
fader.addEventListener('blur', () => { clearTimeout(keyTimer); letGo(); });

// --- render loop ------------------------------------------------------------
const fmt = (s) => `${Math.floor(Math.max(0, s) / 60)}:${String(Math.floor(Math.max(0, s) % 60)).padStart(2, '0')}`;
// Per-frame text writes only when the string changes: a write invalidates layout, and a few
// dozen of them per frame would cost the main thread the audio engine also runs on.
const textCache = new Map();
function setText(sel, s) {
  if (textCache.get(sel) === s) return;
  textCache.set(sel, s);
  const el = typeof sel === 'string' ? $(sel) : sel;
  if (el) el.textContent = s;
}
const shown = new Map();
function show(sel, on) {
  if (shown.get(sel) === on) return;
  shown.set(sel, on);
  $(sel).hidden = !on;
}
function setRole(i, text, cls) {
  const el = $(`#deck${i} .role`);
  const k = `${text}|${cls}`;
  if (el.dataset.k === k) return;
  el.dataset.k = k;
  el.textContent = text;
  el.className = `role ${cls}`;
}

const pickNow = () => S.pickDeck({ live: live(), cur: M.cur, plan: M.plan, now: M.ctx ? now() : 0 });

const lanes = [0, 1].map((i) => new Lane($(`#deck${i} canvas`), i));
// Lane view offset (left edge - now), eased toward its target over ~the handover token
// (600 ms) so the view pans after a mix instead of jumping. Reduced motion: it jumps.
let viewOff = null, lastTs = 0;
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');

function frame(ts) {
  const dt = Math.min(0.1, (ts - (lastTs || ts)) / 1000);
  lastTs = ts;
  if (live()) liveFrame(dt); else idleFrame();
  // The deck buttons name the deck a pick lands on; it flips at each swap and when a mix
  // passes the replan margin, neither of which is a server state change. setPick is a no-op
  // unless it changed.
  setPick(pickNow());
  requestAnimationFrame(frame);
}

/** Before Play: lane A shows the opener at its start (or resume) point, static, so it's drawn
 * only when something it shows changes (opener, start point, lane size), not 60 times a second. */
let idleKey = '';
function idleFrame() {
  const info = O.info;
  const startAt = O.at ?? (info ? S.firstOffset(info) : 0);
  const key = [info?.id, startAt, lanes[0].w, lanes[0].h, lanes[1].w, lanes[1].h].join('|');
  if (key === idleKey) return;
  idleKey = key;
  const view = { now: 0, left: laneLeft(0) };
  lanes[0].setInfo(info);
  lanes[0].draw({ zero: -startAt, startAt: null }, view, null, info ? { end: info.duration_rendered } : {});
  lanes[1].setInfo(null);
  lanes[1].draw({ zero: 0, startAt: null }, view, null, {}, true);
}

function liveFrame(dt) {
  const t = now(), p = M.plan;
  // ON AIR moves to the incoming halfway through a transition, when it's the louder one
  // (deck roles only swap at the very end, which looked like the old song hung on).
  const onAir = p && t >= p.T0 + p.length / 2 ? p.inDeck : M.cur;
  const anchor = p ?? (M.lastPlan && t < M.lastPlan.end + 2 ? M.lastPlan : null);
  const target = laneLeft(t, anchor?.T0 ?? null) - t;
  viewOff = viewOff == null || reduceMotion.matches ? target : viewOff + (target - viewOff) * (1 - Math.exp(-dt / 0.15));
  const view = { now: t, left: t + viewOff };
  const mixing = p && t >= p.T0;
  const label = p ? FX_LABEL[p.kind] ?? p.kind : '';
  const inc = p ? M.decks[p.inDeck] : null;
  M.decks.forEach((d, i) => {
    const started = d.started(t);
    let role = ['', ''];
    if (!d.info) role = ['empty', ''];
    else if (i === onAir && started) role = ['on air', 'on-air'];
    else if (p && i === p.inDeck) role = [isBeatmatched(p.kind) ? `cued · matched to ${'AB'[1 - i]}` : 'cued', ''];
    else if (p && started && mixing) role = ['fading out', ''];
    else if (!d.busy) role = ['empty', '']; // stopped or dropped: nothing on this deck now
    setRole(i, ...role);
    $(`#deck${i}`).classList.toggle('empty', !d.busy);
    const pos = d.info ? Math.max(0, d.pos(t)) : 0;
    setText(`#deck${i} .time`, d.info ? `${fmt(pos)} / ${fmt(d.info.duration_rendered)}` : '');
    if (d.info) $(`#deck${i} .overview .pos`).style.transform = `translateX(${(Math.min(0.995, pos / d.info.duration_rendered) * 100).toFixed(2)}%)`;
    lanes[i].setInfo(d.info);
    const mix = p ? { plan: p, inDeck: p.inDeck, bar: p.bar, live: mixing,
      label: mixTag(p.kind, label, p.length, p.bar, mixing),
      bassTo: p.kind === 'bass_swap' ? { time: S.bassMid(p.T0, p.length, p.bar), letter: 'AB'[p.inDeck] } : null } : null;
    const marks = p && i !== p.inDeck ? { end: p.end - d.zero } : p ? { start: p.inOffset } : {};
    lanes[i].draw({ zero: d.zero, startAt: d.busy ? d.startAt : null, endAt: d.endAt }, view, mix, marks, i === 1);
  });
  // The fader animates from the same progress the gain curves use.
  if (!M.manual && p) {
    const f = S.faderAt(p, t);
    fader.value = Math.round((p.inDeck === 1 ? f : 1 - f) * 1000);
  }
  setText('#xf-tag', M.manual ? 'MANUAL' : 'AUTO');
  $('#xf-tag').classList.toggle('manual', M.manual);
  show('#xf-manual', M.manual);
  updateSign(t, p, mixing, inc, onAir, label);
  updateFx(t);
  saveResume(t);
}

/** The ON AIR sign: what's next and when, the running mix, or the end of the queue. */
function updateSign(t, p, mixing, inc, onAir, label) {
  const s = getState(), cur = M.decks[M.cur].info, airInfo = M.decks[onAir]?.info ?? cur;
  const rating = M.rate && Date.now() < M.rate.until && !(p && t >= p.T0 - 12);
  if (M.rate && !rating && Date.now() >= M.rate.until) M.rate = null;
  const pairs = s?.pairs ?? {};
  let side = null;
  if (M.manual) {
    setText('#sign-kicker', "You're mixing");
    setText('#sign-title', airInfo?.title ?? '—');
    setText('#sign-sub', 'Auto paused for this mix');
  } else if (p && !mixing) {
    const loved = isLoved(pairOf(pairs, cur?.id, p.res.in_deck.id));
    setKicker(loved ? 'loved' : 'next');
    setText('#sign-title', p.res.in_deck.title);
    setText('#sign-sub', `${p.res.in_deck.channel}  ·  ${label.toLowerCase()} out of ${cur?.title ?? ''}`);
    setText('#countdown', fmt(p.T0 - t));
    side = rating && p.T0 - t > 12 ? 'rate' : 'next';
  } else if (p && mixing) {
    setKicker('mixing');
    setText('#sign-title', p.res.in_deck.title);
    const after = s?.queue.find((q) => q.id !== p.res.in_deck.id && q.id !== cur?.id);
    const outro = p.res.in_deck.sections_rendered?.at(-1);
    setText('#sign-sub', [p.res.in_deck.channel, after ? `then ${after.title}${outro ? ` in ~${fmt(outro.start - p.inOffset)}` : ''}` : null]
      .filter(Boolean).join('  ·  '));
    const g = mixProgress(t, p.T0, p.length, p.bar);
    setText('#mix-name', label);
    setText('#mix-progress', g.bars ? `bar ${g.bar} of ${g.bars}  ·  ${fmt(g.left)} left` : `${fmt(g.left)} left`);
    // Bar segments and icon change once per bar / per mix: rewrite only then, not every frame.
    const bars = $('#mix-bars'), n = Math.min(g.bars, 16), bk = `${n}|${g.bar}`;
    if (bars.dataset.k !== bk) {
      bars.dataset.k = bk;
      if (bars.childElementCount !== n) bars.replaceChildren(...Array.from({ length: n }, () => document.createElement('i')));
      [...bars.children].forEach((b, k) => { b.className = k < g.bar - 1 ? 'done' : k === g.bar - 1 ? 'now' : ''; });
    }
    if (textCache.get('mix-shape') !== p.kind) { textCache.set('mix-shape', p.kind); $('#mix-shape').replaceChildren(fxShape(p.kind)); }
    side = 'mix';
  } else {
    const rest = s?.queue.filter((q) => q.id !== cur?.id) ?? [];
    if (!rest.length && !M.req?.loading && cur) {
      setKicker('end');
      setText('#sign-title', `${cur.title} is the last song`);
      setText('#sign-sub', `${cur.channel}  ·  ${fmt(cur.duration_rendered - M.decks[M.cur].pos(t))} left  ·  then silence`);
      if (shown.get('#side-help') !== true) {
        const flavor = s?.flavors?.[0];
        helpButtons([
          ...(s?.lastfm && !s.party && flavor ? [['sparkles', `Turn on Party: ${flavor[0].toUpperCase()}${flavor.slice(1)}`, () => api('/api/party', 'POST', { flavor }).then(setState, report)]] : []),
          ['sparkles', 'Build a mood', () => { toTab('vibe'); $('#genres .build')?.click(); }],
          ['link', 'Add link', () => openAdd()]]);
      }
      side = rating ? 'rate' : 'help';
    } else {
      setKicker('now');
      setText('#sign-title', cur?.title ?? '—');
      setText('#sign-sub', cur?.channel ?? '');
      side = rating ? 'rate' : null;
    }
  }
  $('#sign').classList.toggle('mixing', side === 'mix');
  show('#side-idle', false);
  show('#side-next', side === 'next');
  show('#side-mix', side === 'mix');
  show('#side-rate', side === 'rate');
  show('#side-help', side === 'help');
  show('.sign-side', side !== null);
  if (side === 'rate') {
    setText('#rate-pair', M.rate.title);
    $('#rate-timer').style.setProperty('--left', String(Math.max(0, (M.rate.until - Date.now()) / RATE_MS)));
  }
  // Phone: the THEN line and the now bar on the other tabs.
  const nextIn = p?.res.in_deck;
  const then = nextIn && s?.queue.find((q) => q.id !== nextIn.id && q.id !== cur?.id);
  show('#then', !!then);
  if (then) {
    setText('#then b', `${then.title} — ${then.channel}`);
    show('#then .badge', mixesWith(nextIn.bpm_playing, then.bpm));
  }
  setText('#nb-title', airInfo?.title ?? '—');
  setText('#nb-sub', p ? (mixing ? `Mixing into ${p.res.in_deck.title}` : `${p.res.in_deck.title} in ${fmt(p.T0 - t)}`) : '');
  const canMix = !!M.decks[M.cur]?.started(t) && !M.manual && !(p && t >= p.T0 - S.REPLAN_MARGIN);
  if ($('#mixnow').disabled === canMix) { $('#mixnow').disabled = !canMix; $('#nb-mix').disabled = !canMix; }
}

function setKicker(kind) {
  if (textCache.get('kicker') === kind) return;
  textCache.set('kicker', kind);
  const k = $('#sign-kicker');
  textCache.delete('#sign-kicker');
  k.className = `kicker${kind === 'mixing' ? ' live' : ''}`;
  if (kind === 'loved') {
    const l = document.createElement('span');
    l.className = 'loved';
    l.append(icon('flame'), 'a pair you loved');
    k.replaceChildren('Next mix ', l);
  } else k.textContent = { next: 'Next mix', mixing: 'Mixing into', end: 'End of queue', now: 'Now playing' }[kind];
}

// --- live FX: filter knob + echo throw ----------------------------------------
function liveFx(ctx, input) {
  const hp = new BiquadFilterNode(ctx, { type: 'highpass', frequency: S.HP_IDLE });
  const lp = new BiquadFilterNode(ctx, { type: 'lowpass', frequency: S.LP_IDLE });
  const send = new GainNode(ctx, { gain: 0 });
  const delay = new DelayNode(ctx, { maxDelayTime: 2, delayTime: 0.5 });
  const fb = new GainNode(ctx, { gain: S.ECHO_FEEDBACK });
  input.connect(hp).connect(lp).connect(ctx.destination);
  lp.connect(send).connect(delay).connect(fb).connect(delay);
  delay.connect(ctx.destination);
  return { hp, lp, send, delay };
}
const filterKnob = $('#live-filter');
filterKnob.addEventListener('input', () => {
  const x = filterKnob.valueAsNumber / 100;
  filterKnob.setAttribute('aria-valuetext', x === 0 ? 'off' : x < 0 ? `lowpass ${Math.round(-x * 100)}%` : `highpass ${Math.round(x * 100)}%`);
  if (!M.live) return;
  // One knob, DJ-mixer style: left of centre = lowpass closing, right = highpass opening.
  const t = now();
  const lp = x < 0 ? S.LP_IDLE * (200 / S.LP_IDLE) ** -x : S.LP_IDLE;
  const hp = x > 0 ? S.HP_IDLE * (4000 / S.HP_IDLE) ** x : S.HP_IDLE;
  M.live.lp.frequency.setTargetAtTime(lp, t, 0.02);
  M.live.hp.frequency.setTargetAtTime(hp, t, 0.02);
});
filterKnob.addEventListener('dblclick', () => { filterKnob.value = 0; filterKnob.dispatchEvent(new Event('input')); });
const throwBtn = $('#live-echo');
const echoOn = (on) => {
  if (!M.live) return;
  const t = now(), d = M.decks[M.cur];
  M.live.delay.delayTime.setValueAtTime(60 / (d?.info?.bpm_playing || 120) / 2, t); // 1/8 note
  M.live.send.gain.setTargetAtTime(on ? 1 : 0, t, 0.01);
  throwBtn.setAttribute('aria-pressed', String(on));
};
// Hold to throw: echoes keep ringing out after release, like a DJ mixer's echo button.
throwBtn.addEventListener('pointerdown', () => echoOn(true));
for (const ev of ['pointerup', 'pointerleave', 'pointercancel']) throwBtn.addEventListener(ev, () => echoOn(false));
throwBtn.addEventListener('keydown', (e) => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); echoOn(true); } });
throwBtn.addEventListener('keyup', () => echoOn(false));
throwBtn.addEventListener('contextmenu', (e) => e.preventDefault()); // long-press on a phone

// --- effect picker --------------------------------------------------------------
// A listbox, not a <select>: it lists only the effects the server says work for this pair
// (res.options), with icons. Server-held choice (/api/next_effect), shared with the MCP DJ.
const fxBtn = $('#fx-btn'), fxList = $('#fx-list');
let fxActive = 0;
/** Rebuilt only when the plan changes (not every frame) and never while open. */
function updateFx(t) {
  const p = M.plan;
  const locked = p && t >= p.T0 - S.REPLAN_MARGIN; // too late to change it
  show('#fx-btn', !!p && !locked && !M.manual);
  show('#fx-locked', !!p && locked && t < p.T0);
  if (p && locked) setText('#fx-locked span', `${FX_LABEL[p.kind] ?? p.kind} · locked`);
  if (locked && !fxList.hidden) closeFx(false);
  if (!p || p === M.fxPlan || !fxList.hidden) return;
  M.fxPlan = p;
  const opts = p.res.options ?? [];
  const cur = M.force && opts.includes(M.force) ? M.force : '';
  // Pair memory: Auto names the effect the owner loved for this pair; one they 👎'd here stays
  // pickable but dimmed and labelled, so it's clear why Auto didn't choose it.
  const auto = autoLabel(p.kind, p.res.loved_effect), bad = p.res.avoid_effect;
  // The button is short ("🔥 Auto · Bass swap"); the list says why.
  setText('#fx-label', cur ? FX_LABEL[cur] : autoLabel(p.kind, p.res.loved_effect, true));
  $('#fx-icon').setAttribute('href', `#i-${cur ? FX_ICON[cur] : p.res.loved_effect ? 'flame' : 'sparkles'}`);
  fxList.replaceChildren(...[['', auto], ...opts.map((k) => [k, fxOptionLabel(k, bad)])].map(([k, text], i) => {
    const li = document.createElement('li');
    li.id = `fx-opt-${i}`;
    if (k && k === bad) li.className = 'avoided';
    // One text node group: the li is a flex row, so a bare inline icon would get the row's gap.
    const span = document.createElement('span');
    span.append(...withThumbs(text));
    li.setAttribute('role', 'option');
    li.dataset.k = k;
    li.setAttribute('aria-selected', String(k === cur));
    // Effects show their shape (design plxFh); Auto keeps its sparkles / loved flame.
    li.append(k ? fxShape(k) : icon(p.res.loved_effect ? 'flame' : 'sparkles'), span);
    if (k === cur) li.append(icon('check', 'check'));
    li.onclick = () => chooseFx(k);
    return li;
  }));
}
function openFx() {
  fxList.hidden = false;
  fxBtn.setAttribute('aria-expanded', 'true');
  fxActive = Math.max(0, [...fxList.children].findIndex((li) => li.getAttribute('aria-selected') === 'true'));
  markFx();
  fxList.focus({ preventScroll: true });
}
function closeFx(refocus = true) {
  fxList.hidden = true;
  fxBtn.setAttribute('aria-expanded', 'false');
  M.fxPlan = null; // rebuild with the latest options now it's closed
  if (refocus) fxBtn.focus();
}
function markFx() {
  [...fxList.children].forEach((li, i) => li.classList.toggle('active', i === fxActive));
  fxList.setAttribute('aria-activedescendant', `fx-opt-${fxActive}`);
}
function chooseFx(k) {
  closeFx();
  if (!M.plan || !S.canReplan(now(), M.plan.T0)) { status('Too late to change this mix.'); return; }
  // Stored on the server (shared with the MCP DJ); onState sees the change and replans.
  api('/api/next_effect', 'POST', { effect: k || null }).then(setState).catch((e) => status(e.message));
}
fxBtn.addEventListener('click', () => (fxList.hidden ? openFx() : closeFx()));
fxList.addEventListener('keydown', (e) => {
  const n = fxList.children.length;
  if (e.key === 'ArrowDown') fxActive = (fxActive + 1) % n;
  else if (e.key === 'ArrowUp') fxActive = (fxActive - 1 + n) % n;
  else if (e.key === 'Home') fxActive = 0;
  else if (e.key === 'End') fxActive = n - 1;
  else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); chooseFx(fxList.children[fxActive].dataset.k); return; }
  else if (e.key === 'Escape' || e.key === 'Tab') { closeFx(e.key === 'Escape'); return; }
  else return;
  e.preventDefault();
  markFx();
});
document.addEventListener('pointerdown', (e) => { if (!fxList.hidden && !e.target.closest('.fx-wrap')) closeFx(false); });

// --- rating the last hand-over ---------------------------------------------------
async function rate(verdict) {
  const r = M.rate;
  M.rate = null;
  if (!r) return;
  try { setState(await api('/api/pair', 'POST', { from: r.from, to: r.to, verdict, effect: r.effect })); } catch (e) { report(e); }
}
// Phone ✕: dismiss without rating (Played still has the rating later).
$('#rate-close').addEventListener('click', () => { M.rate = null; });
$('#rate-love').addEventListener('click', () => rate('loved'));
$('#rate-avoid').addEventListener('click', () => rate('avoided'));

// --- wiring -------------------------------------------------------------------------
function openAdd() { $('#add-btn').click(); }
function focusSearch() { toTab('search'); $('#q').focus(); }
$('#play').addEventListener('click', play);
$('#mixnow').addEventListener('click', mixNow);
$('#nb-mix').addEventListener('click', mixNow);
$('#b2b').addEventListener('click', async () => {
  const on = $('#b2b').getAttribute('aria-checked') !== 'true';
  try { setState(await api('/api/mode', 'POST', { beat_to_beat: on })); } catch (e) { report(e); }
});
$('#choose-btn').addEventListener('click', () => {
  if (live()) return;
  const f = $('#op-find'); f.scrollIntoView({ block: 'center' }); f.focus();
});
$('#alt-btn').addEventListener('click', () => {
  if (live()) return; // opener actions only exist before Play; never touch deck A while it plays
  if (O.resume) { O.at = O.at != null ? null : Math.max(0, O.resume.pos - 1); renderIdle(); return; }
  // Next song in Up next after the current opener (wraps around).
  const q = getState()?.queue ?? [];
  const i = q.findIndex((x) => x.id === O.id);
  if (q.length > 1) loadOpener(q[(i + 1) % q.length], null, true);
});
document.addEventListener('keydown', (e) => {
  const typing = e.target.closest?.('input, select, textarea, button, [role="listbox"], [contenteditable]');
  // Space plays (before the first song), "/" jumps to search: the design's keyboard hints.
  if (e.key === ' ' && !typing && !live() && O.id) { e.preventDefault(); play(); }
  if (e.key === '/' && !e.target.closest?.('input, textarea, select')) { e.preventDefault(); focusSearch(); }
});
initOnboard({ api, setOpener, last: getState });
initUi((s) => { renderOnboard(s); renderMcp(s); onState(s); }, {
  loadOpener: (song) => { if (!live()) loadOpener(song, null, true); },
  isLive: live,
  // Playlist rows say "now playing on A" / "next on deck B".
  decks: {
    of: (id) => { const i = live() ? M.decks.findIndex((d) => d.busy && d.info?.id === id) : -1; return i < 0 ? null : 'AB'[i]; },
    // Server's next song = where a pick lands: the idle deck, or after a running mix the one that frees up.
    next: () => (!live() ? null : 'AB'[pickNow().deck]),
  },
});
initMcp();
// Canvas text (NOW tag, END/START) is drawn in Plex Mono: redraw the idle lanes once it has loaded.
document.fonts?.ready.then(() => { idleKey = ''; });
requestAnimationFrame(frame);
poll();
