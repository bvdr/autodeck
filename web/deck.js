// One deck: audio graph + its lane's text (title, BPM, key). Applies plans built by
// schedule.js. The waveform lives in lanes.js (both decks share one time axis).
//
// Graph (built once per deck, a new BufferSource per song):
//   source -> main ---\
//   vocals -> voc ----+-> highpass -> lowpass -> gate -> gain -> master
//   instr. -> inst ---/
// The stem sources (vocals / instrumental renders on the same time map) only exist once loaded
// (Voice/Beat switch, Vocal swap); they start sample-aligned with the song and the three gains
// pick what's heard (schedule.modeGains). Every effect below them works the same on any mode.
//                                              \-> echoSend -> delay <-> feedback(0.5)
//                                              |               delay -> master
//                                              \-> revSend -> convolver (3 s tail) -> master
// Effect sources (loop roll chunks, backspin) enter at the highpass like the song does;
// the riser is its own noise source straight to master.
// The delay sits after the gain so an echo-out echoes the *fading* signal and its
// tail keeps ringing after the dry signal is gone.
import { ECHO_FEEDBACK, HP_IDLE, LP_IDLE, STEM_FADE, modeGains, loopPos, loopZero, lastPassAt, exitLoop } from './schedule.js';
import { overview } from './lanes.js';

/** setTimeout in AudioContext time. While the context is suspended (Pause) its clock stands
 * still, so a wall-clock timer would fire early: this one re-arms for whatever ctx time is
 * still left. `keep(id)` gets each timer id, so the caller can clear the live one. */
export function ctxTimeout(ctx, t, fn, keep) {
  const arm = () => keep(setTimeout(() => (t - ctx.currentTime > 0.01 ? arm() : fn()), Math.max(0, t - ctx.currentTime) * 1000));
  arm();
}

/** Stereo decaying-noise impulse: a plain 3 s hall for the reverb wash. */
function impulse(ctx, seconds = 3) {
  const n = Math.floor(ctx.sampleRate * seconds), b = ctx.createBuffer(2, n, ctx.sampleRate);
  for (let c = 0; c < 2; c++) {
    const d = b.getChannelData(c);
    for (let i = 0; i < n; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / n) ** 3;
  }
  return b;
}

const fmtBpm = (b) => (Number.isFinite(b) ? b.toFixed(1) : '?');

/** Lane text for a deck payload (/api/deck or a transition's in_deck): "Pop  100.0→101.7  E maj · 12B".
 * Shared with the pre-play preview, which has a payload but no playing deck. */
export function laneText(root, i) {
  const q = (sel) => root.querySelector(sel);
  q('.title').textContent = i ? i.title : '—';
  q('.artist').textContent = i ? i.channel : '';
  // speed_at_start * bpm = the tempo it starts at (matched to the outgoing); bpm_playing =
  // where it settles after the ease-back. Arrow = original → playing, as in the design.
  const playing = i ? i.bpm_original * i.speed_at_start : NaN;
  const key = i ? [i.key, i.camelot].filter(Boolean).join(' · ') : '';
  q('.metaline').textContent = i ? [i.genre, `${fmtBpm(i.bpm_original)}→${fmtBpm(playing)}`, key].filter(Boolean).join('  ') : '';
  q('.metaline').title = i && Math.abs(i.bpm_playing - playing) > 0.05 ? `Settles at ${fmtBpm(i.bpm_playing)} BPM` : '';
  const ov = q('.overview'), pos = ov.querySelector('.pos');
  // Buttons: on a live deck a section loops (ON AIR) or picks the entry (cued); app.js enables them.
  ov.replaceChildren(...(i ? overview(i.sections_rendered, i.duration_rendered) : []).map((x, k) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.disabled = true;
    b.dataset.i = k;
    b.setAttribute('aria-label', x.label);
    b.className = x.label;
    b.style.flex = String(Math.max(0.001, x.to - x.from));
    return b;
  }), pos);
  root.classList.toggle('empty', !i);
}

/** Fetch + decode a deck payload's audio. Runs before the deck slot is free. */
export async function loadTrack(ctx, info) {
  const r = await fetch(info.url);
  if (!r.ok) throw new Error(`audio ${r.status}`);
  return { info, buffer: await ctx.decodeAudioData(await r.arrayBuffer()) };
}

export class Deck {
  constructor(ctx, master, root) {
    this.ctx = ctx;
    this.root = root;
    this.hp = new BiquadFilterNode(ctx, { type: 'highpass', frequency: HP_IDLE });
    this.lp = new BiquadFilterNode(ctx, { type: 'lowpass', frequency: LP_IDLE });
    this.gate = new GainNode(ctx, { gain: 1 });
    this.gain = new GainNode(ctx, { gain: 0 });
    this.revSend = new GainNode(ctx, { gain: 0 });
    this.verb = new ConvolverNode(ctx, { buffer: impulse(ctx) });
    this.master = master;
    this.extra = []; // effect sources scheduled for the current transition
    this.send = new GainNode(ctx, { gain: 0 });
    // 2 s max delay = one beat down to 30 BPM.
    this.delay = new DelayNode(ctx, { maxDelayTime: 2, delayTime: 0.5 });
    this.fb = new GainNode(ctx, { gain: ECHO_FEEDBACK });
    this.hp.connect(this.lp).connect(this.gate).connect(this.gain).connect(master);
    this.mix = { main: new GainNode(ctx, { gain: 1 }), voc: new GainNode(ctx, { gain: 0 }), inst: new GainNode(ctx, { gain: 0 }) };
    for (const g of Object.values(this.mix)) g.connect(this.hp);
    this.mode = 'both';  // the Voice/Beat switch; back to 'both' with every new song
    this.stems = {};     // 'voc' | 'inst' -> AudioBufferSourceNode of that stem's render
    this.gain.connect(this.revSend).connect(this.verb).connect(master);
    this.gain.connect(this.send).connect(this.delay).connect(this.fb).connect(this.delay);
    this.delay.connect(master);
    this.src = null;
    this.track = null;
    // ctx time of rendered second 0. With a section loop: of the audio after the loop (what plans
    // and effects schedule against); pos() knows where the playhead really is inside the loop.
    this.zero = 0;
    this.loop = null;    // schedule.js loop object (setLoop), null = plays straight through
    this.loopTimer = 0;
    this.startAt = 0;    // ctx time the source starts sounding
    this.endAt = Infinity; // ctx time the source stops (stopAt), for the lane drawing
  }

  get info() { return this.track?.info ?? null; }
  /** True while a source exists that hasn't been stopped (scheduled or sounding). */
  get busy() { return this.src !== null; }
  started(now) { return this.busy && now >= this.startAt; }
  pos(now) { return this.loop ? loopPos(this.loop, now) : now - this.zero; }

  param(name) {
    if (name in this.mix) return this.mix[name].gain;
    return { gain: this.gain.gain, hp: this.hp.frequency, echo: this.send.gain, delay: this.delay.delayTime,
      lp: this.lp.frequency, gate: this.gate.gain, rev: this.revSend.gain,
      // Playback rate belongs to the current source (brake); a fresh source starts at 1.
      rate: this.src?.playbackRate ?? new GainNode(this.ctx).gain }[name];
  }

  /** Put a decoded track on this deck and schedule it: source.start(when, offset). */
  start(track, when, offset, onEnded) {
    this.stop();
    this.track = track;
    const src = new AudioBufferSourceNode(this.ctx, { buffer: track.buffer });
    src.connect(this.mix.main);
    this.mode = 'both';
    for (const [k, v] of Object.entries(modeGains('both'))) { const p = this.mix[k].gain; p.cancelScheduledValues(0); p.setValueAtTime(v, 0); }
    src.onended = () => {
      if (this.src === src) { this.src = null; this.endAt = Math.min(this.endAt, this.ctx.currentTime); onEnded?.(this); }
    };
    src.start(when, offset);
    this.src = src;
    this.zero = when - offset;
    this.startAt = when;
    this.endAt = Infinity;
    this.planId = null; // /api/transition plan that brought this song in (null for the first song)
    this.renderInfo();
  }

  /** The song's sources: the main render and any loaded stems (all aligned, all stop together). */
  sources() { return this.src ? [this.src, ...Object.values(this.stems)] : []; }

  /** Stop in ctx time (sample-accurate), not via a wall-clock timer. */
  stopAt(when) { for (const s of this.sources()) s.stop(when); if (this.src) this.endAt = when; }

  /** Undo a stopAt (and any scheduled effect sources). The spec applies only the last
   * stop() call made before the source has stopped, so a far-future stop cancels the earlier one. */
  unstop() { for (const s of this.sources()) s.stop(this.ctx.currentTime + 1e7); this.endAt = Infinity; this.dropExtra(); }

  /** Start a stem render (same time map as the song) aligned with the playing source, silent until
   * its gain rises. `name` 'voc' | 'inst'. A stem already loaded for this song is kept. */
  addStem(name, buffer, now) {
    if (!this.src || this.stems[name]) return;
    const s = new AudioBufferSourceNode(this.ctx, { buffer });
    s.connect(this.mix[name]);
    // Lead so start() is in the future; pos() maps that ctx time to the rendered second (loop-aware),
    // which is exactly where the main source will be then: same buffer timeline, same sample.
    const when = Math.max(now + 0.05, this.startAt);
    if (this.loop) { s.loopStart = this.loop.start; s.loopEnd = this.loop.end; s.loop = this.src.loop; }
    s.playbackRate.value = this.src.playbackRate.value;
    s.start(when, Math.max(0, this.pos(when)));
    if (Number.isFinite(this.endAt)) s.stop(this.endAt);
    this.stems[name] = s;
  }

  /** The Voice/Beat switch: a STEM_FADE crossfade from wherever the gains are now. */
  setMode(mode, now) {
    this.mode = mode;
    for (const [k, v] of Object.entries(modeGains(mode))) {
      const p = this.mix[k].gain;
      p.cancelScheduledValues(now);
      p.setValueAtTime(p.value, now);
      p.linearRampToValueAtTime(v, now + STEM_FADE);
    }
  }

  dropExtra() {
    for (const s of this.extra) { try { s.stop(); } catch { /* not started */ } s.disconnect(); }
    this.extra = [];
  }

  stop() {
    this.clearLoop();
    const s = this.src;
    this.src = null; // first, so onended doesn't report a deliberate stop as "song ended"
    if (s) this.endAt = Math.min(this.endAt, this.ctx.currentTime);
    for (const x of [s, ...Object.values(this.stems)]) if (x) { try { x.stop(); } catch { /* never started */ } x.disconnect(); }
    this.stems = {};
    this.dropExtra();
  }

  /** Repeat a section: the source's native loop (sample-accurate wraps at loopEnd -> loopStart).
   * `l` from schedule.makeLoop / exitLoop. Passing a section that's still ahead is fine: the
   * source plays into it and wraps at its end. */
  setLoop(l, now) {
    if (!this.src) return;
    clearTimeout(this.loopTimer);
    this.loop = l;
    // "Until I stop" has no end yet; plans aren't made while it runs (app.js), so this zero is unused.
    this.zero = l.n === Infinity ? l.z0 : loopZero(l);
    const src = this.src, all = this.sources();
    for (const x of all) { x.loopStart = l.start; x.loopEnd = l.end; x.loop = l.n > 1; }
    // The only non-sample-accurate step: turning the loop flag off. It just has to happen during
    // the last pass (after its wrap, before its end), so it fires just after that pass starts and
    // has the whole pass (8+ bars) of slack for timer jitter / background-tab throttling.
    if (l.n > 1 && l.n !== Infinity) {
      ctxTimeout(this.ctx, lastPassAt(l) + 0.25, () => { if (this.src === src) for (const x of this.sources()) x.loop = false; },
        (id) => { this.loopTimer = id; });
    }
  }

  /** Finish the pass playing now, then carry on. */
  exitLoop(now) {
    if (!this.loop) return;
    const l = exitLoop(this.loop, now);
    if (l.n > 1) { this.setLoop(l, now); return; }
    // Already the last pass: nothing left to loop, the song plays on from here.
    this.clearLoop();
    this.zero = l.z0;
  }

  clearLoop() {
    clearTimeout(this.loopTimer);
    if (this.loop) for (const x of this.sources()) x.loop = false;
    this.loop = null;
  }

  /** Extra source playing part of this deck's buffer into the deck chain. */
  chunk(buffer, when, offset, duration, rate = 1) {
    const s = new AudioBufferSourceNode(this.ctx, { buffer, playbackRate: rate });
    s.connect(this.hp);
    s.start(when, Math.max(0, offset), duration);
    this.extra.push(s);
    return s;
  }

  /** Effects that need their own sources (schedule.js events with param 'fx'). */
  fx(e) {
    const buf = this.track.buffer, pos = e.time - this.zero; // rendered second at T0
    if (e.type === 'roll') {
      // The bar before T0 repeats: half-bar slices over the first half, quarter-bar slices
      // over the second, each restarting on the grid (sample-accurate start(when, offset)).
      // ponytail: the slices are of the full mix even in Voice/Beat mode; slice the stem if missed.
      for (const x of this.sources()) x.stop(e.time);
      const from = pos - e.bar;
      for (const [slice, span] of [[e.bar / 2, e.duration / 2], [e.bar / 4, e.duration / 2]]) {
        const t0 = e.time + (slice === e.bar / 2 ? 0 : e.duration / 2);
        for (let x = 0; x < span - 1e-6; x += slice) this.chunk(buf, t0 + x, from, slice);
      }
    } else if (e.type === 'backspin') {
      // The last ~1.5 bars played backwards, speeding up, fading out.
      for (const x of this.sources()) x.stop(e.time);
      const sr = buf.sampleRate, len = Math.floor(Math.min(1.5 * e.bar, pos) * sr), start = Math.floor(pos * sr) - len;
      const rev = this.ctx.createBuffer(buf.numberOfChannels, Math.max(1, len), sr);
      for (let c = 0; c < buf.numberOfChannels; c++) {
        const src = buf.getChannelData(c).subarray(Math.max(0, start), start + len);
        rev.getChannelData(c).set(Float32Array.from(src).reverse());
      }
      const s = this.chunk(rev, e.time, 0, e.duration);
      s.playbackRate.setValueAtTime(1, e.time);
      s.playbackRate.linearRampToValueAtTime(3, e.time + e.duration);
      this.gain.gain.setValueAtTime(1, e.time);
      this.gain.gain.linearRampToValueAtTime(0, e.time + e.duration);
    } else if (e.type === 'riser') {
      // Filtered white noise sweeping up and swelling over the outgoing's last bars.
      const sr = this.ctx.sampleRate, noise = this.ctx.createBuffer(1, sr * 2, sr), d = noise.getChannelData(0);
      for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
      const s = new AudioBufferSourceNode(this.ctx, { buffer: noise, loop: true });
      const bp = new BiquadFilterNode(this.ctx, { type: 'bandpass', frequency: 400, Q: 1.5 });
      const g = new GainNode(this.ctx, { gain: 0 });
      s.connect(bp).connect(g).connect(this.master);
      bp.frequency.setValueAtTime(400, e.time);
      bp.frequency.exponentialRampToValueAtTime(9000, e.time + e.duration);
      g.gain.setValueAtTime(0, e.time);
      g.gain.linearRampToValueAtTime(0.25, e.time + e.duration * 0.95);
      g.gain.linearRampToValueAtTime(0, e.time + e.duration);
      s.start(e.time);
      s.stop(e.time + e.duration);
      this.extra.push(s);
    }
  }

  /** Apply schedule.js events. Each touched param is cleared from `now` first; plans
   * always begin a param with a `set` at now, so nothing is left undefined. */
  apply(events, now) {
    for (const name of new Set(events.map((e) => e.param))) if (name !== 'fx') this.params(name).forEach((p) => p.cancelScheduledValues(now));
    for (const e of events) {
      if (e.param === 'fx') { this.fx(e); continue; }
      for (const p of this.params(e.param)) {
        if (e.type === 'set') p.setValueAtTime(e.value, e.time);
        else if (e.type === 'linear') p.linearRampToValueAtTime(e.value, e.time);
        else if (e.type === 'exp') p.exponentialRampToValueAtTime(e.value, e.time);
        else if (e.type === 'curve') p.setValueCurveAtTime(e.values, e.time, e.duration);
      }
    }
  }

  /** The AudioParams an event drives: 'rate' (brake) drives every source of the song, so loaded
   * stems slow down with it and stay aligned; everything else is one param. */
  params(name) {
    return name === 'rate' && this.src ? this.sources().map((s) => s.playbackRate) : [this.param(name)];
  }

  /** Manual takeover: freeze every param where it is right now. cancelAndHoldAtTime keeps
   * the mid-curve value (Chrome/Safari); cancelScheduledValues alone would jump back to
   * the last event's value, so the fallback re-sets the value we read. */
  hold(now) {
    for (const name of ['gain', 'hp', 'echo', 'main', 'voc', 'inst']) {
      const p = this.param(name);
      if (p.cancelAndHoldAtTime) p.cancelAndHoldAtTime(now);
      else { const v = p.value; p.cancelScheduledValues(now); p.setValueAtTime(v, now); }
    }
    // The fader owns the sound now: no filters, sends, chops or effect sources.
    this.hp.frequency.setTargetAtTime(HP_IDLE, now, 0.05);
    this.lp.frequency.cancelScheduledValues(now);
    this.lp.frequency.setTargetAtTime(LP_IDLE, now, 0.05);
    this.gate.gain.cancelScheduledValues(now);
    this.gate.gain.setTargetAtTime(1, now, 0.01);
    this.send.gain.setTargetAtTime(0, now, 0.05);
    this.revSend.gain.cancelScheduledValues(now);
    this.revSend.gain.setTargetAtTime(0, now, 0.05);
    for (const s of this.sources()) { s.playbackRate.cancelScheduledValues(now); s.playbackRate.setValueAtTime(1, now); }
    this.dropExtra();
  }

  /** Fader-driven gain; a short time constant avoids zipper noise from input events. */
  setGain(v, now) { this.gain.gain.setTargetAtTime(v, now, 0.015); }

  renderInfo() { laneText(this.root, this.info); }
}
