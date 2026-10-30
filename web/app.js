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
  force: null,     // owner's forced effect for the current song's exit (null = app's pick)
  fxPlan: null,    // the plan the effect dropdown was last built for
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
  localStorage.setItem(RESUME_KEY, JSON.stringify({ id: d.info.id, title: d.info.title, pos: d.pos(t), at: lastSave }));
}
const FX_LABEL = { blend: 'Beat blend', half_double: 'Beat blend (½×/2×)', bass_swap: 'Bass swap',
  filter_sweep: 'Filter sweep', echo: 'Echo out', loop_roll: 'Loop roll', brake: 'Brake', backspin: 'Backspin',
  reverb: 'Reverb wash', gate: 'Gate / stutter', riser: 'Riser → drop', dropswap: 'Drop swap', cut: 'Cut', fade: 'Fade' };

const now = () => M.ctx.currentTime;
const status = (t) => { $('#status').textContent = t; };
const at = (ctxTime, fn) => M.timers.push(setTimeout(fn, Math.max(0, ctxTime - now()) * 1000));
const clearTimers = () => { M.timers.forEach(clearTimeout); M.timers = []; };
const curId = () => M.decks[M.cur].info?.id;

// --- transitions ------------------------------------------------------------
/** Ask the server for the transition out of `deck`'s song. `after` = rendered seconds. */
async function requestPlan(deck, after = null, retries = 0) {
  const d = M.decks[deck], id = d.info.id, seq = ++M.seq;
  M.req = { seq, deck, id, after, version: getState()?.version, mode: !!getState()?.beat_to_beat,
            inId: undefined, retries, loading: true };
  status('Preparing next mix…');
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
      status(`Next mix failed (${e.message}) — retrying…`);
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
    // No mix was scheduled (server was down, or the plan never arrived): don't go silent,
    // start the next queued song straight away. Only a truly empty queue stops the music.
    const next = getState()?.queue.find((q) => q.id !== deck.info?.id);
    if (next) {
      startSong(next).catch((e) => { status(e.message); $('#play').disabled = false; });
      return;
    }
    status('Stopped — end of queue. Add songs and press Play to start again.');
    $('#play').disabled = false;
  }
  tryApply(); // a slot just freed: a waiting plan may now be scheduled
}

// --- poll + replan ------------------------------------------------------------
function onState(s) {
  // Server-held controls (set from the dropdown, the API or the MCP DJ):
  // a mix-now request and the forced effect for the current song's exit.
  if (M.mixSeq === undefined) M.mixSeq = s.mix_now_seq;
  else if (s.mix_now_seq > M.mixSeq) { M.mixSeq = s.mix_now_seq; if (M.ctx) mixNow(); }
  if ((s.next_effect ?? null) !== M.force) {
    M.force = s.next_effect ?? null;
    M.fxPlan = null;
    if (M.ctx && M.req && !M.req.loading && (!M.plan || S.canReplan(now(), M.plan.T0))) requestPlan(M.cur, M.req.after);
  }
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
      // Live performance FX on the master bus, after both decks, so the owner's hands never
      // fight a scheduled transition's automation (which lives on the deck nodes).
      M.live = liveFx(M.ctx, master);
      M.decks = [0, 1].map((i) => new Deck(M.ctx, master, $(`#deck${i}`)));
      requestAnimationFrame(frame);
    }
    await M.ctx.resume();
    const s = await api('/api/state');
    setState(s);
    // Resume the song that was playing before the reload, at its position. The server's
    // session still holds that song's time map, so /api/deck returns the same render.
    const res = savedResume();
    const known = res && s.pool.some((p) => p.id === res.id && p.status === 'ready');
    const first = known ? { id: res.id, title: res.title } : s.queue[0];
    if (!first) throw new Error('Nothing ready to play yet — add songs below.');
    await startSong(first, known ? res.pos - 1 : null);
  } catch (e) {
    status(e.message);
    $('#play').disabled = false;
  }
}

/** Start `song` from scratch on deck 0 (Play, Resume, or recovering from a missed mix).
 * `at` = rendered seconds to start from (resume), null = the song's own start. */
async function startSong(song, at = null) {
  status(`Loading ${song.title}…`);
  const info = await api(`/api/deck/${song.id}`);
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

function frame() {
  const t = now();
  // ON AIR moves to the incoming halfway through a transition, when it's the louder one
  // (deck roles only swap at the very end, which looked like the old song hung on).
  const onAir = M.plan && t >= M.plan.T0 + M.plan.length / 2 ? M.plan.inDeck : M.cur;
  M.decks.forEach((d, i) => {
    d.draw(t);
    d.root.classList.toggle('on-air', i === onAir && d.started(t));
    d.root.querySelector('.time').textContent = d.info ? `${fmt(d.pos(t))} / ${fmt(d.info.duration_rendered)}` : '';
  });
  // The fader animates from the same progress the gain curves use.
  if (!M.manual && M.plan) {
    const p = S.faderAt(M.plan, t);
    fader.value = Math.round((M.plan.inDeck === 1 ? p : 1 - p) * 1000);
    if (t < M.plan.T0) status(`Next: ${M.plan.res.in_deck.title} in ${fmt(M.plan.T0 - t)}`);
    else status(`Mixing into ${M.plan.res.in_deck.title} (${FX_LABEL[M.plan.kind] ?? M.plan.kind})`);
  }
  updateFx(t);
  saveResume(t);
  requestAnimationFrame(frame);
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
  if (!M.live) return;
  // One knob, DJ-mixer style: left of centre = lowpass closing, right = highpass opening.
  const x = filterKnob.valueAsNumber / 100, t = now();
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

// --- effect dropdown --------------------------------------------------------
const fx = $('#fx');
/** Show the next mix's effect; rebuilt only when the plan changes (not every frame). */
function updateFx(t) {
  const p = M.plan;
  $('#fx-wrap').hidden = !p || M.manual || t >= p.T0 - S.REPLAN_MARGIN; // too late to change it
  if (!p || p === M.fxPlan || document.activeElement === fx) return;
  M.fxPlan = p;
  const opts = p.res.options ?? [];
  fx.replaceChildren(
    new Option(`Auto (${FX_LABEL[p.kind] ?? p.kind})`, ''),
    ...opts.map((k) => new Option(FX_LABEL[k], k)));
  fx.value = M.force && opts.includes(M.force) ? M.force : '';
}
fx.addEventListener('change', () => {
  if (!M.plan || !S.canReplan(now(), M.plan.T0)) { status('Too late to change this mix.'); return; }
  // Stored on the server (shared with the MCP DJ); onState sees the change and replans.
  api('/api/next_effect', 'POST', { effect: fx.value || null }).then(setState).catch((e) => status(e.message));
});

$('#play').addEventListener('click', play);
$('#mixnow').addEventListener('click', mixNow);
$('#b2b').addEventListener('click', async () => {
  const on = $('#b2b').getAttribute('aria-pressed') !== 'true';
  try { setState(await api('/api/mode', 'POST', { beat_to_beat: on })); } catch (e) { $('#msg').textContent = e.message; }
});
initUi(onState);
{
  const r = savedResume();
  if (r) $('#play').textContent = `▶ Resume ${r.title.length > 28 ? `${r.title.slice(0, 27)}…` : r.title} at ${fmt(r.pos)}`;
}
poll();
