// Pure timing + gain-curve math for transitions (Ruling E). No DOM, no AudioContext:
// every function takes plain numbers/objects and returns plain data, so it runs under
// `node --test web/schedule.test.mjs`. app.js/deck.js turn the returned plans into
// real AudioParam calls.
//
// Time vocabulary used throughout:
//   ctx time  = AudioContext.currentTime seconds.
//   rendered  = seconds into a deck's rendered WAV (what the server's *_rendered fields use).
//   zero      = the ctx time at which a deck's rendered second 0 plays (startTime - offset),
//               so ctx(t_rendered) = zero + t_rendered.

// Contract T9->T10: a new /api/transition may replace a scheduled plan only while
// now < T0 - REPLAN_MARGIN. Rendering takes a few seconds, so a replan requested any
// later could land after T0 and the old plan would already be audible.
export const REPLAN_MARGIN = 1.5;
// A plan whose T0 is closer than this can't be scheduled reliably (setTimeout jitter,
// AudioParam events must be strictly in the future); we refetch with `after` instead.
export const MIN_LEAD = 0.25;
// "Mix now" asks for the first phrase boundary after pos + PREP_LEAD: the render
// takes seconds, and a boundary inside that window would already be gone on arrival.
export const PREP_LEAD = 4;
export const HP_IDLE = 20;       // Hz: highpass this low is inaudible, i.e. "off"
export const HP_BLEND = 300;     // Hz: incoming starts bass-less so two basslines don't stack
export const ECHO_FEEDBACK = 0.5;
export const LP_IDLE = 20000;    // Hz: lowpass this high is "off"
export const BASS_CUT = 250;     // Hz: highpass that removes a song's bass (bass swap)
export const SWEEP_HP = 1500;    // Hz: incoming starts this thin in a filter sweep
export const SWEEP_LP = 300;     // Hz: outgoing ends this muffled in a filter sweep
export const CURVE_POINTS = 64;  // enough for a smooth cos/sin; setValueCurveAtTime interpolates

const QUICK = new Set(['cut', 'dropswap']); // instant swaps: their `length` is not a crossfade
const EQUAL_POWER = new Set(['blend', 'half_double']);
// Exit effects: the outgoing does its thing for `length`, then the incoming lands.
export const EXIT = new Set(['echo', 'loop_roll', 'brake', 'backspin', 'reverb', 'gate', 'riser']);

/** Equal-power crossfade curves: out = cos, in = sin, so out² + in² = 1 at every point. */
export function equalPower(n = CURVE_POINTS) {
  const out = new Float32Array(n), inn = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const a = (i / (n - 1)) * Math.PI / 2;
    out[i] = Math.cos(a);
    inn[i] = Math.sin(a);
  }
  return { out, in: inn };
}

/** The rendered second a fresh deck should start at: its trimmed play_start, which the
 * server exposes as the start of the first section (sections are clamped to play_start). */
export function firstOffset(deck) {
  const s = deck.sections_rendered?.[0]?.start;
  return Number.isFinite(s) ? s : (deck.downbeats_rendered?.[0] ?? 0);
}

/** Where the transition sits in ctx time. `res` is the /api/transition response. */
export function timing(outZero, res) {
  const { kind, length } = res.transition;
  const T0 = outZero + res.out_at_rendered;
  // Exit effects (echo, roll, brake…): the outgoing plays out over `length`, then the incoming lands.
  const inStart = EXIT.has(kind) ? T0 + length : T0;
  const len = QUICK.has(kind) ? 0 : length;
  return { kind, T0, length: len, inStart, inOffset: res.in_at_rendered,
           inZero: inStart - res.in_at_rendered,
           // Deck roles swap (and the outgoing source is stopped) here.
           end: T0 + len };
}

const set = (param, value, time) => ({ param, type: 'set', value, time });
const lin = (param, value, time) => ({ param, type: 'linear', value, time });

/**
 * Full automation plan for both decks as plain data. Event types:
 *   set    -> setValueAtTime(value, time)
 *   linear -> linearRampToValueAtTime(value, time)
 *   exp    -> exponentialRampToValueAtTime(value, time)
 *   curve  -> setValueCurveAtTime(values, time, duration)
 * params: gain | hp (highpass frequency) | echo (send into the delay loop) | delay (delayTime).
 * Every param a plan touches starts with a `set` at `now` so the applier can
 * cancelScheduledValues(now) first and still leave a defined value. Requires now < T0.
 * `beat` = outgoing seconds per beat (echo delay time).
 */
export function plan(res, outZero, now, beat = 0.5) {
  const t = timing(outZero, res);
  const { kind, T0, length: L } = t;
  const bar = 4 * beat;
  const out = [set('gain', 1, now)];
  // The incoming deck may carry a previous song's effect state: every send/filter back to idle.
  const inn = [set('hp', HP_IDLE, now), set('echo', 0, now), set('lp', LP_IDLE, now),
    set('gate', 1, now), set('rev', 0, now)];
  const fx = (type, extra = {}) => ({ param: 'fx', type, time: T0, duration: L, bar, beat, ...extra });
  if (EQUAL_POWER.has(kind)) {
    const c = equalPower();
    out.push({ param: 'gain', type: 'curve', values: c.out, time: T0, duration: L });
    inn.push(set('gain', 0, now), { param: 'gain', type: 'curve', values: c.in, time: T0, duration: L },
      // Bass comes back over the first half; exp ramp because pitch perception is logarithmic.
      set('hp', HP_BLEND, T0), { param: 'hp', type: 'exp', value: HP_IDLE, time: T0 + L / 2 });
  } else if (kind === 'bass_swap') {
    // Both songs at full level; only one has bass. The bass jumps across on the bar line
    // halfway through, so two basslines never play together (the classic club blend).
    const mid = T0 + Math.max(1, Math.round(L / 2 / bar)) * bar;
    out.push(set('hp', HP_IDLE, now), set('hp', BASS_CUT, mid), set('gain', 1, mid), lin('gain', 0, T0 + L));
    inn.push(set('gain', 0, now), set('gain', 0, T0), lin('gain', 1, mid),
      set('hp', BASS_CUT, T0), set('hp', HP_IDLE, mid));
  } else if (kind === 'filter_sweep') {
    // Outgoing is slowly muffled (lowpass down) while the incoming opens up from thin (highpass down).
    const c = equalPower();
    out.push(set('lp', LP_IDLE, now), set('lp', LP_IDLE, T0), { param: 'lp', type: 'exp', value: SWEEP_LP, time: T0 + L },
      set('gain', 1, T0 + 0.75 * L), lin('gain', 0, T0 + L));
    inn.push(set('gain', 0, now), { param: 'gain', type: 'curve', values: c.in, time: T0, duration: L },
      set('hp', SWEEP_HP, T0), { param: 'hp', type: 'exp', value: HP_IDLE, time: T0 + L });
  } else if (kind === 'loop_roll') {
    // The bar before T0 repeats in halves, then quarters, then the incoming drops (deck.fx).
    out.push(fx('roll'));
    inn.push(set('gain', 1, now));
  } else if (kind === 'brake') {
    // Vinyl stop: playback slows to a halt (pitch drops with it, on purpose).
    out.push(set('rate', 1, now), set('rate', 1, T0), lin('rate', 0.05, T0 + L),
      set('gain', 1, T0 + 0.6 * L), lin('gain', 0, T0 + L));
    inn.push(set('gain', 1, now));
  } else if (kind === 'backspin') {
    out.push(fx('backspin'));
    inn.push(set('gain', 1, now));
  } else if (kind === 'reverb') {
    // Dissolve into a reverb tail; the tail keeps ringing after the dry signal is gone.
    out.push(set('rev', 0, now), set('rev', 0, T0), lin('rev', 1, T0 + L / 4),
      set('gain', 1, T0), lin('gain', 0, T0 + L));
    inn.push(set('gain', 1, now));
  } else if (kind === 'gate') {
    // Stutter: the outgoing chops on 1/8 notes, then the incoming drops.
    const step = beat / 2;
    out.push(set('gate', 1, now));
    for (let x = T0; x < T0 + L - 1e-6; x += step) out.push(set('gate', 1, x), set('gate', 0, x + step / 2));
    out.push(set('gain', 0, T0 + L));
    inn.push(set('gain', 1, now));
  } else if (kind === 'riser') {
    // White-noise riser builds over the outgoing's last bars, then a hard cut into the drop.
    out.push(fx('riser'), set('gain', 1, now), set('gain', 0, T0 + L));
    inn.push(set('gain', 1, now));
  } else if (kind === 'fade') {
    out.push(set('gain', 1, T0), lin('gain', 0, T0 + L));
    inn.push(set('gain', 0, now), set('gain', 0, T0), lin('gain', 1, T0 + L));
  } else if (kind === 'echo') {
    // Delay sits after the deck gain, so the echoes are of the fading signal and the
    // feedback tail keeps ringing (0.5 per beat) after the dry signal is gone.
    out.push(set('delay', beat, now), set('echo', 0, now), set('echo', 1, T0),
      set('gain', 1, T0), lin('gain', 0, T0 + res.transition.length));
    inn.push(set('gain', 1, now)); // source is silent until inStart = T0 + length anyway
  } else { // cut, dropswap: instant swap on the boundary (dropswap's offset is b's drop)
    out.push(set('gain', 0, T0));
    inn.push(set('gain', 1, now));
  }
  return { ...t, out, in: inn };
}

/** Every deck effect control at its idle value ("no effect"), gain at 0. */
export function idle(now) {
  return [set('gain', 0, now), set('hp', HP_IDLE, now), set('lp', LP_IDLE, now), set('gate', 1, now),
    set('echo', 0, now), set('rev', 0, now)];
}

/** Crossfader progress 0 (all outgoing) .. 1 (all incoming) at ctx time `now`. The
 * UI maps this to the fader; for blends the gains are cos/sin of the same value. */
export function faderAt(p, now) {
  if (p.length === 0) return now < p.T0 ? 0 : 1;
  return Math.min(1, Math.max(0, (now - p.T0) / p.length));
}

/** Manual fader: x = 0 is deck 0 (left), 1 is deck 1. Same equal-power law as blends. */
export function manualGains(x) {
  const a = Math.min(1, Math.max(0, x)) * Math.PI / 2;
  return [Math.cos(a), Math.sin(a)];
}

/** Manual release: the deck the fader is nearer to wins, but only if it is actually
 * playing (a scheduled-but-not-started deck can't take over). Exact middle keeps `cur`. */
export function release(x, cur, started) {
  const nearer = x < 0.5 ? 0 : x > 0.5 ? 1 : cur;
  const winner = started[nearer] ? nearer : cur;
  return { winner, loser: 1 - winner };
}

/** After release: loser fades to 0 and winner rises to 1, both over one bar. */
export function releaseRamps(now, bar, gains, winner) {
  const loser = 1 - winner;
  return {
    [winner]: [set('gain', gains[winner], now), lin('gain', 1, now + bar)],
    [loser]: [set('gain', gains[loser], now), lin('gain', 0, now + bar)],
  };
}

/** Outgoing deck when a scheduled plan is dropped. Before T0 nothing is audible yet, so
 * undo the scheduled fade (back to full, echo send closed). From T0 on (e.g. grabbing the
 * fader mid echo-out) the fade is already audible: null = hold where it is, no snap to 1. */
export function outgoingOnCancel(now, T0) {
  return now < T0 ? [set('gain', 1, now), set('echo', 0, now), set('lp', LP_IDLE, now),
    set('gate', 1, now), set('rev', 0, now), set('rate', 1, now)] : null;
}

// Keyboard fader: arrow keys fire input/change per step, so "release" is this long
// without a key step (or blur), not every keyup.
export const KEY_RELEASE_MS = 600;

export const canReplan = (now, T0, margin = REPLAN_MARGIN) => now < T0 - margin;
export const tooSoon = (T0, now) => T0 < now + MIN_LEAD;

/**
 * Poll-driven replan check (contract: latest response wins, never after T0 - margin).
 * Only a change of the song that would come next can change the transition
 * (pick_transition depends on (a, b, after, entered)), so we skip the multi-second
 * re-render when the version bump was e.g. a download finishing deep in the window.
 */
export function shouldReplan({ stateVersion, planVersion, planInId, nextId, now, T0 = Infinity }) {
  if (stateVersion === planVersion) return false;
  if ((nextId ?? null) === (planInId ?? null)) return false;
  return canReplan(now, T0);
}

/** Worst |incoming downbeat - nearest outgoing downbeat| in ms over [from, to] ctx time.
 * null when the incoming has no downbeat in the window (nothing to compare). */
export function driftMs(outDownbeats, inDownbeats, outZero, inZero, from, to) {
  const outs = outDownbeats.map((d) => outZero + d);
  let worst = null;
  for (const d of inDownbeats) {
    const t = inZero + d;
    if (t < from - 1e-6 || t > to + 1e-6) continue;
    let best = Infinity;
    for (const o of outs) best = Math.min(best, Math.abs(o - t));
    worst = Math.max(worst ?? 0, best * 1000);
  }
  return worst;
}
