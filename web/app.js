// Mixer state machine: which deck is on air, the scheduled transition, replanning,
// Mix now and the manual crossfader. Timing math lives in schedule.js (pure, tested),
// Web Audio plumbing in deck.js, everything else on the page in ui.js.
import * as S from './schedule.js';
import { Deck, loadTrack } from './deck.js';
import { api, initUi, setState, getState } from './ui.js';

const $ = (sel) => document.querySelector(sel);
const POLL_MS = 2000;
const MAX_RETRIES = 3; // tooSoon refetches before giving up and letting the song play out

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
};

const now = () => M.ctx.currentTime;
const status = (t) => { $('#status').textContent = t; };
const at = (ctxTime, fn) => M.timers.push(setTimeout(fn, Math.max(0, ctxTime - now()) * 1000));
const clearTimers = () => { M.timers.forEach(clearTimeout); M.timers = []; };
const curId = () => M.decks[M.cur].info?.id;

// --- transitions ------------------------------------------------------------
/** Ask the server for the transition out of `deck`'s song. `after` = rendered seconds. */
async function requestPlan(deck, after = null, retries = 0) {
  const d = M.decks[deck], id = d.info.id, seq = ++M.seq;
  M.req = { seq, deck, id, after, version: getState()?.version, inId: undefined, retries, loading: true };
  status('Preparing next mix…');
  let res;
  try {
    const q = new URLSearchParams({ from: id });
    if (after != null) q.set('after', after.toFixed(3));
    res = await api(`/api/transition?${q}`);
    if (res.transition) res.track = await loadTrack(M.ctx, res.in_deck); // decode before scheduling
  } catch (e) {
    if (seq === M.seq) { M.req.loading = false; M.req.inId = null; status(`Next mix failed: ${e.message}`); }
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
    if (!M.plan) status('End of queue — this song plays to its end.');
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
    else status('No usable mix point left — this song plays to its end.');
    return;
  }
  M.next = null;
  M.plan = { ...p, res: n, inDeck: 1 - M.cur };
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
  M.plan = null;
  // The old source stops itself (stopAt in tryApply); its onended frees the slot.
  M.decks[old].param('echo').setValueAtTime(0, now());
  M.cur = 1 - old;
  if (M.req?.loading) status('Preparing next mix…'); // frame() stops updating status once M.plan is gone
  tryApply(); // the new current's exit may already be fetched
}

function songEnded(deck) {
  if (M.decks.indexOf(deck) === M.cur && !M.plan) {
    status('Stopped — end of queue. Add songs and press Play to start again.');
    $('#play').disabled = false;
  }
  tryApply(); // a slot just freed: a waiting plan may now be scheduled
}

// --- poll + replan ------------------------------------------------------------
function onState(s) {
  const r = M.req;
  if (!M.ctx || !r || r.loading || M.manual || r.id !== M.decks[r.deck].info?.id) return;
  const nextId = s.queue.find((q) => q.id !== r.id)?.id;
  const T0 = M.plan && M.plan.res.id === r.id ? M.plan.T0 : undefined;
  // After the outgoing blend started (T0 reached) the contract forbids refetching.
  if (S.shouldReplan({ stateVersion: s.version, planVersion: r.version, planInId: r.inId, nextId, now: now(), T0 })) {
    requestPlan(r.deck, r.after);
  } else {
    r.version = s.version; // seen; don't re-evaluate until the next change
  }
}

async function poll() {
  try { setState(await api('/api/state')); } catch { /* server restarting; next poll retries */ }
  setTimeout(poll, POLL_MS);
}

// --- controls ---------------------------------------------------------------
async function play() {
  $('#play').disabled = true;
  try {
    // Autoplay policy: the AudioContext must be created inside the click.
    if (!M.ctx) {
      M.ctx = new AudioContext();
      const master = new GainNode(M.ctx);
      master.connect(M.ctx.destination);
      M.decks = [0, 1].map((i) => new Deck(M.ctx, master, $(`#deck${i}`)));
      requestAnimationFrame(frame);
    }
    await M.ctx.resume();
    const s = await api('/api/state');
    setState(s);
    const first = s.queue[0];
    if (!first) throw new Error('Nothing ready to play yet — add songs below.');
    status(`Loading ${first.title}…`);
    const info = await api(`/api/deck/${first.id}`);
    const track = await loadTrack(M.ctx, info);
    M.decks.forEach((d) => d.stop());
    if (M.plan) cancelPlan();
    M.cur = 0;
    const d = M.decks[0];
    d.apply([{ param: 'gain', type: 'set', value: 1, time: now() }], now());
    d.start(track, now() + 0.1, S.firstOffset(info), songEnded);
    onStarted(0);
  } catch (e) {
    status(e.message);
    $('#play').disabled = false;
  }
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
  status('Manual crossfader — let go and the nearer deck takes over.');
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
fader.addEventListener('pointerdown', (e) => { pointerHeld = true; try { fader.setPointerCapture(e.pointerId); } catch { /* pointer already gone */ } takeOver(); });
fader.addEventListener('pointerup', () => { pointerHeld = false; letGo(); });
fader.addEventListener('input', () => {
  drive();
  // Keyboard (no pointer held): release after a pause, not on every keyup, so a run of
  // arrow presses is one gesture instead of a snap + refetch per step.
  if (!pointerHeld) { clearTimeout(keyTimer); keyTimer = setTimeout(letGo, S.KEY_RELEASE_MS); }
});
fader.addEventListener('blur', () => { clearTimeout(keyTimer); letGo(); });

// --- render loop ------------------------------------------------------------
const fmt = (s) => `${Math.floor(Math.max(0, s) / 60)}:${String(Math.floor(Math.max(0, s) % 60)).padStart(2, '0')}`;

function frame() {
  const t = now();
  M.decks.forEach((d, i) => {
    d.draw(t);
    d.root.classList.toggle('on-air', i === M.cur && d.started(t));
    d.root.querySelector('.time').textContent = d.info ? `${fmt(d.pos(t))} / ${fmt(d.info.duration_rendered)}` : '';
  });
  // The fader animates from the same progress the gain curves use.
  if (!M.manual && M.plan) {
    const p = S.faderAt(M.plan, t);
    fader.value = Math.round((M.plan.inDeck === 1 ? p : 1 - p) * 1000);
    if (t < M.plan.T0) status(`Next: ${M.plan.res.in_deck.title} — ${M.plan.kind} in ${fmt(M.plan.T0 - t)}`);
    else status(`Mixing into ${M.plan.res.in_deck.title} (${M.plan.kind})`);
  }
  requestAnimationFrame(frame);
}

$('#play').addEventListener('click', play);
$('#mixnow').addEventListener('click', mixNow);
initUi(onState);
poll();
