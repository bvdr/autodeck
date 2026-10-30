// One deck: audio graph + waveform canvas. Applies plans built by schedule.js.
//
// Graph (built once per deck, a new BufferSource per song):
//   source -> highpass -> lowpass -> gate -> gain -> master
//                                              \-> echoSend -> delay <-> feedback(0.5)
//                                              |               delay -> master
//                                              \-> revSend -> convolver (3 s tail) -> master
// Effect sources (loop roll chunks, backspin) enter at the highpass like the song does;
// the riser is its own noise source straight to master.
// The delay sits after the gain so an echo-out echoes the *fading* signal and its
// tail keeps ringing after the dry signal is gone.
import { ECHO_FEEDBACK, HP_IDLE, LP_IDLE } from './schedule.js';

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
    this.canvas = root.querySelector('canvas');
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
    this.gain.connect(this.revSend).connect(this.verb).connect(master);
    this.gain.connect(this.send).connect(this.delay).connect(this.fb).connect(this.delay);
    this.delay.connect(master);
    this.src = null;
    this.track = null;
    this.zero = 0;       // ctx time of rendered second 0
    this.startAt = 0;    // ctx time the source starts sounding
    this.wave = null;    // static waveform layer, redrawn only when the song changes
  }

  get info() { return this.track?.info ?? null; }
  /** True while a source exists that hasn't been stopped (scheduled or sounding). */
  get busy() { return this.src !== null; }
  started(now) { return this.busy && now >= this.startAt; }
  pos(now) { return now - this.zero; }

  param(name) {
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
    src.connect(this.hp);
    src.onended = () => { if (this.src === src) { this.src = null; onEnded?.(this); } };
    src.start(when, offset);
    this.src = src;
    this.zero = when - offset;
    this.startAt = when;
    this.wave = null;
    this.planId = null; // /api/transition plan that brought this song in (null for the first song)
    this.renderInfo();
  }

  /** Stop in ctx time (sample-accurate), not via a wall-clock timer. */
  stopAt(when) { this.src?.stop(when); }

  /** Undo a stopAt (and any scheduled effect sources). The spec applies only the last
   * stop() call made before the source has stopped, so a far-future stop cancels the earlier one. */
  unstop() { this.src?.stop(this.ctx.currentTime + 1e7); this.dropExtra(); }

  dropExtra() {
    for (const s of this.extra) { try { s.stop(); } catch { /* not started */ } s.disconnect(); }
    this.extra = [];
  }

  stop() {
    const s = this.src;
    this.src = null; // first, so onended doesn't report a deliberate stop as "song ended"
    if (s) { try { s.stop(); } catch { /* never started */ } s.disconnect(); }
    this.dropExtra();
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
      this.src.stop(e.time);
      const from = pos - e.bar;
      for (const [slice, span] of [[e.bar / 2, e.duration / 2], [e.bar / 4, e.duration / 2]]) {
        const t0 = e.time + (slice === e.bar / 2 ? 0 : e.duration / 2);
        for (let x = 0; x < span - 1e-6; x += slice) this.chunk(buf, t0 + x, from, slice);
      }
    } else if (e.type === 'backspin') {
      // The last ~1.5 bars played backwards, speeding up, fading out.
      this.src.stop(e.time);
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
    for (const name of new Set(events.map((e) => e.param))) if (name !== 'fx') this.param(name).cancelScheduledValues(now);
    for (const e of events) {
      if (e.param === 'fx') { this.fx(e); continue; }
      const p = this.param(e.param);
      if (e.type === 'set') p.setValueAtTime(e.value, e.time);
      else if (e.type === 'linear') p.linearRampToValueAtTime(e.value, e.time);
      else if (e.type === 'exp') p.exponentialRampToValueAtTime(e.value, e.time);
      else if (e.type === 'curve') p.setValueCurveAtTime(e.values, e.time, e.duration);
    }
  }

  /** Manual takeover: freeze every param where it is right now. cancelAndHoldAtTime keeps
   * the mid-curve value (Chrome/Safari); cancelScheduledValues alone would jump back to
   * the last event's value, so the fallback re-sets the value we read. */
  hold(now) {
    for (const name of ['gain', 'hp', 'echo']) {
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
    if (this.src) { this.src.playbackRate.cancelScheduledValues(now); this.src.playbackRate.setValueAtTime(1, now); }
    this.dropExtra();
  }

  /** Fader-driven gain; a short time constant avoids zipper noise from input events. */
  setGain(v, now) { this.gain.gain.setTargetAtTime(v, now, 0.015); }

  renderInfo() {
    const i = this.info;
    const q = (sel) => this.root.querySelector(sel);
    q('.title').textContent = i ? i.title : '—';
    q('.artist').textContent = i ? i.channel : '';
    q('.genre').textContent = i?.genre ?? '';
    const playing = i ? i.bpm_original * i.speed_at_start : NaN;
    // bpm_playing is where the tempo settles after the ease-back; show it when it differs.
    const settle = i && Math.abs(i.bpm_playing - playing) > 0.05 ? ` (settles ${fmtBpm(i.bpm_playing)})` : '';
    q('.bpm').textContent = i ? `${fmtBpm(i.bpm_original)} → ${fmtBpm(playing)} BPM${settle}` : '';
    q('.key').textContent = i ? [i.key, i.camelot].filter(Boolean).join(' · ') : '';
  }

  /** Static layer: peaks, sections, downbeats. Peaks are of the source audio's played span
   * [play_start, play_end] (the server slices them) and are stretched over the rendered duration. ponytail: tempo-eased stretches are drawn
   * linearly (sub-second error); map peaks through the time map if it ever matters. */
  drawStatic(w, h) {
    const c = new OffscreenCanvas(w, h), g = c.getContext('2d'), i = this.info;
    const dur = i.duration_rendered || 1, X = (t) => (t / dur) * w;
    const tint = { intro: '#1d3b4d', body: '#1b1f2a', outro: '#3d2a1d' };
    for (const s of i.sections_rendered) {
      g.fillStyle = tint[s.label] ?? '#1b1f2a';
      g.fillRect(X(s.start), 0, Math.max(1, X(s.end) - X(s.start)), h);
    }
    g.fillStyle = '#7fb4ff';
    const pk = i.peaks, n = pk.length;
    for (let x = 0; x < w && n; x++) {
      const v = pk[Math.min(n - 1, Math.floor((x / w) * n))] * (h / 2 - 2);
      g.fillRect(x, h / 2 - v, 1, Math.max(1, v * 2));
    }
    g.fillStyle = 'rgba(255,255,255,0.25)';
    for (const d of i.downbeats_rendered) g.fillRect(Math.round(X(d)), 0, 1, 6);
    return c;
  }

  draw(now) {
    const cv = this.canvas, g = cv.getContext('2d');
    const w = cv.clientWidth, h = cv.clientHeight;
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; this.wave = null; }
    g.clearRect(0, 0, w, h);
    if (!this.info || !w) return;
    if (!this.wave) this.wave = this.drawStatic(w, h);
    g.drawImage(this.wave, 0, 0);
    const x = (this.pos(now) / (this.info.duration_rendered || 1)) * w;
    g.fillStyle = '#ffcc33';
    g.fillRect(Math.round(Math.min(w - 2, Math.max(0, x))), 0, 2, h);
  }
}
