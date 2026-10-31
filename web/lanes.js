// The two-lane timeline: both decks' waveforms on one shared time axis, a NOW line, the
// transition window with its gain curves, and the bass hand-over marker.
//
// Top half: pure geometry (no DOM, node-tested in lanes.test.mjs). Bottom half: Lane, one
// canvas per deck. Performance (the audio engine shares the main thread): each song's
// waveform is drawn ONCE into an offscreen bitmap at the lane's pixels-per-second; a frame is
// a couple of drawImage crops plus a few dozen rects. Canvas sizes come from a
// ResizeObserver, never from reading layout inside the frame loop (that would force a
// synchronous layout after the frame's text updates: layout thrash).
import { EXIT } from './schedule.js';

export const VIEW_SPAN = 64;   // seconds across a lane (design: ~24 s countdown = 37 % of the width)
export const NOW_AT = 0.1;     // NOW sits 10 % in from the left when nothing is being mixed
export const MIX_AT = 0.25;    // a mix window is kept at least this far in, so it's never cut off
export const NOW_MAX = 0.85;   // ...but NOW never goes further right than this
const BEATMATCHED = new Set(['blend', 'half_double', 'bass_swap', 'filter_sweep']);

/** Left edge (ctx time) of the lane view. Normal: NOW at 10 %. With a mix coming up or under
 * way, the view holds back so the window starts at least 25 % in (design R3rrm: NOW walks
 * through the window instead of the window sliding off the left edge). */
export function laneLeft(now, T0 = null, span = VIEW_SPAN) {
  let left = now - NOW_AT * span;
  if (T0 != null) left = Math.min(left, T0 - MIX_AT * span);
  return Math.max(left, now - NOW_MAX * span);
}

/** ctx time -> x pixel in a lane `width` px wide showing [left, left + span]. */
export const xAt = (t, left, width, span = VIEW_SPAN) => ((t - left) / span) * width;

/** Value of a deck's `gain` at ctx time t, from schedule.plan() events (the same events the
 * deck's AudioParam got), so the drawn curve is exactly what is heard. Web Audio semantics:
 * a linear/exp ramp runs from the previous event's time and value. exp is drawn as linear. */
// A plan's events never change, and the lanes sample each curve ~65 times a frame: filter and
// sort the gain events once per events array, not once per sample.
const gainCache = new WeakMap();
export function gainAt(events, t) {
  let lastT = -Infinity, lastV = 0;
  let gains = gainCache.get(events);
  if (!gains) {
    gains = events.filter((e) => e.param === 'gain').sort((a, b) => a.time - b.time);
    gainCache.set(events, gains);
  }
  for (const e of gains) {
    if (e.type === 'set') {
      if (e.time > t) return lastV;
      lastT = e.time; lastV = e.value;
    } else if (e.type === 'linear' || e.type === 'exp') {
      if (t < e.time) return lastT === -Infinity ? lastV : lastV + (e.value - lastV) * (t - lastT) / (e.time - lastT);
      lastT = e.time; lastV = e.value;
    } else if (e.type === 'curve') {
      if (t < e.time) return lastV;
      const n = e.values.length, end = e.time + e.duration;
      if (t < end) {
        const f = ((t - e.time) / e.duration) * (n - 1), i = Math.floor(f);
        return e.values[i] + (e.values[Math.min(n - 1, i + 1)] - e.values[i]) * (f - i);
      }
      lastT = end; lastV = e.values[n - 1];
    }
  }
  return lastV;
}

/** Whole bars a transition lasts (the "8 BARS" in the tag); 0 for an instant swap. */
export const mixBars = (length, bar) => (length > 0 ? Math.max(1, Math.round(length / bar)) : 0);

/** Progress through a running mix for the sign: "bar 5 of 8 · 0:09 left". */
export function mixProgress(now, T0, length, bar) {
  const bars = mixBars(length, bar);
  const into = Math.max(0, now - T0);
  return { bar: Math.min(bars, Math.floor(into / bar) + 1), bars, left: Math.max(0, length - into) };
}

/** The deck overview strip: intro / main / outro as fractions of the rendered song. */
export function overview(sections, duration) {
  if (!sections?.length || !(duration > 0)) return [];
  const f = (t) => Math.min(1, Math.max(0, t / duration));
  return sections.map((s) => ({ label: s.label === 'body' ? 'main' : s.label, from: f(s.start), to: f(s.end) }));
}

/** Do two tempos beat-match (same rule as the server's 8 % stretch limit, also ½× / 2×)?
 * Used for the "mixes" badge on the phone's THEN line. */
export function mixesWith(a, b, maxPct = 0.08) {
  if (!(a > 0) || !(b > 0)) return false;
  return [1, 2, 0.5].some((k) => Math.abs((b * k) / a - 1) <= maxPct);
}

/** Label for the transition region: "NEXT · BASS SWAP · 8 BARS" before it, without "NEXT"
 * while it plays. */
export function mixTag(kind, label, length, bar, live) {
  const bars = mixBars(length, bar);
  const parts = [label.toUpperCase(), bars ? `${bars} BAR${bars === 1 ? '' : 'S'}` : null].filter(Boolean);
  return (live ? parts : ['NEXT', ...parts]).join(' · ');
}

export const isBeatmatched = (kind) => BEATMATCHED.has(kind);
/** Exit effects start the incoming only after the outgoing's effect: its lane starts later. */
export const incomingStart = (p) => (EXIT.has(p.kind) ? p.T0 + p.length : p.T0);

// --- drawing ---------------------------------------------------------------------
const COL = { a: '#C8F25A', b: '#A98BFF', text: '#F3F1EC', text2: '#ACA8B8', text3: '#8E8A9B',
  bg: '#0D0C10', raised: '#221F29', onAir: '#FF4D2E' };
const MONO = '"IBM Plex Mono", ui-monospace, monospace';
const fmt = (s) => `${Math.floor(Math.max(0, s) / 60)}:${String(Math.floor(Math.max(0, s) % 60)).padStart(2, '0')}`;
const MAX_BITMAP = 16000; // px; canvases this wide are fine everywhere

export class Lane {
  /** `which` 0 = deck A (lime, top lane), 1 = deck B (violet). */
  constructor(canvas, which) {
    this.canvas = canvas;
    this.g = canvas.getContext('2d');
    this.which = which;
    this.color = which === 0 ? COL.a : COL.b;
    this.w = 0; this.h = 0; this.dpr = 1;
    this.info = null; this.wave = null;
    new ResizeObserver(([e]) => {
      const b = e.contentBoxSize?.[0];
      this.w = b ? b.inlineSize : canvas.clientWidth;
      this.h = b ? b.blockSize : canvas.clientHeight;
      this.dpr = Math.min(2, window.devicePixelRatio || 1);
      canvas.width = Math.round(this.w * this.dpr);
      canvas.height = Math.round(this.h * this.dpr);
      this.wave = null; // pixels-per-second changed with the width
    }).observe(canvas);
  }

  setInfo(info) { if (info !== this.info) { this.info = info; this.wave = null; } }

  /** Waveform bitmap of the whole rendered song: rounded columns, a fixed pitch apart. */
  bake() {
    const i = this.info, dur = i.duration_rendered || 1;
    const pps = (this.w / VIEW_SPAN) * this.dpr;
    const width = Math.max(1, Math.min(MAX_BITMAP, Math.ceil(dur * pps)));
    const h = Math.round(this.h * this.dpr);
    const c = new OffscreenCanvas(width, h), g = c.getContext('2d');
    const pitch = (this.w < 500 ? 6 : 8) * this.dpr, bw = pitch / 2;
    const pk = i.peaks ?? [], n = pk.length, mid = h / 2, amp = h * 0.32;
    g.fillStyle = this.color;
    for (let x = 0; x < width && n; x += pitch) {
      // Loudest peak under this column, so short hits don't vanish between columns.
      const a = Math.floor((x / width) * n), b = Math.max(a + 1, Math.floor(((x + pitch) / width) * n));
      let v = 0;
      for (let k = a; k < b && k < n; k++) v = Math.max(v, pk[k]);
      const y = Math.max(1.5 * this.dpr, v * amp);
      g.fillRect(x, mid - y, bw, y * 2);
    }
    return { c, scale: width / dur }; // bitmap px per rendered second
  }

  /**
   * Draw one frame. `d` = the deck (zero, info, started), `view` = { now, left },
   * `mix` = null or { plan, inDeck, label, bar, live } for the scheduled transition,
   * `marks` = { end, start } rendered seconds to label (END on the outgoing, START on the incoming),
   * `nowTag` = draw the NOW tag at the bottom of this lane.
   */
  draw(d, view, mix, marks = {}, nowTag = false) {
    const g = this.g, W = this.w, H = this.h, r = this.dpr;
    if (!W) return;
    g.setTransform(r, 0, 0, r, 0, 0);
    g.clearRect(0, 0, W, H);
    const X = (t) => xAt(t, view.left, W);
    const i = this.info;
    if (i) {
      // Bar ticks (faint, middle band) and phrase ticks (every 8 bars, full height).
      const db = i.downbeats_rendered ?? [];
      for (let k = 0; k < db.length; k++) {
        const x = Math.round(X(d.zero + db[k]));
        if (x < -1 || x > W + 1) continue;
        const phrase = k % 8 === 0;
        g.fillStyle = phrase ? 'rgba(255,255,255,0.10)' : 'rgba(255,255,255,0.04)';
        if (phrase) g.fillRect(x, 0, 1, H); else g.fillRect(x, H * 0.18, 1, H * 0.64);
      }
      if (!this.wave) this.wave = this.bake();
      const { c, scale } = this.wave;
      // Visible rendered-time span of this song, split at NOW: the past is dimmed.
      // endAt: a deck that was stopped (or will stop at the end of the mix) draws nothing after it.
      const t0 = Math.max(0, view.left - d.zero);
      const t1 = Math.min(i.duration_rendered, view.left + VIEW_SPAN - d.zero, (d.endAt ?? Infinity) - d.zero);
      const from = d.startAt != null ? Math.max(t0, d.startAt - d.zero) : t0; // nothing before it starts
      const cut = Math.min(Math.max(view.now - d.zero, from), t1);
      const blit = (a, b, alpha) => {
        if (b <= a) return;
        g.globalAlpha = alpha;
        g.drawImage(c, a * scale, 0, (b - a) * scale, c.height, X(d.zero + a), 0, X(d.zero + b) - X(d.zero + a), H);
      };
      blit(from, cut, 0.4);
      blit(cut, t1, 1);
      g.globalAlpha = 1;
      g.font = `11px ${MONO}`;
      g.fillStyle = COL.text3;
      g.textBaseline = 'middle';
      if (marks.end != null) {
        const x = X(d.zero + marks.end);
        if (x < W - 70) g.fillText(`END ${fmt(marks.end)}`, x + 10, H / 2);
      }
      if (marks.start != null) {
        const x = X(d.zero + marks.start);
        g.textAlign = 'right';
        if (x > 80) g.fillText(`START ${fmt(marks.start)}`, x - 6, H / 2);
        g.textAlign = 'left';
      }
    }
    // NOW: one white line through both lanes (under the mix labels, so they stay readable);
    // its tag sits at the bottom of lane B.
    const nx = Math.round(X(view.now));
    g.fillStyle = COL.text;
    g.fillRect(nx - 1, 0, 2, H);
    if (mix) this.drawMix(mix, X, W, H);
    if (nowTag) this.tag('NOW', nx, H - 22, COL.bg, COL.text, 'center');
  }

  drawMix({ plan: p, inDeck, label, bar, live, bassTo }, X, W, H) {
    const g = this.g;
    const x0 = X(p.T0), x1 = Math.max(X(p.end), X(incomingStart(p)), x0 + 2);
    if (x1 < 0 || x0 > W) return;
    // The window: faint on both lanes, on-air tinted while it plays.
    g.fillStyle = live ? 'rgba(255,77,46,0.10)' : 'rgba(255,255,255,0.03)';
    g.fillRect(x0, 0, x1 - x0, H);
    g.strokeStyle = live ? 'rgba(255,77,46,0.6)' : 'rgba(255,255,255,0.2)';
    g.lineWidth = 1;
    g.strokeRect(Math.round(x0) + 0.5, -1, Math.round(x1 - x0), H + 2);
    // Gain curve of this lane's deck over the window: gain 1 high, 0 low.
    const isIn = this.which === inDeck, events = isIn ? p.in : p.out, inStart = incomingStart(p);
    const y = (v) => H * 0.21 + (1 - v) * H * 0.58;
    const a = p.T0 - bar, b = Math.max(p.end, inStart) + bar, N = 64;
    g.strokeStyle = 'rgba(243,241,236,0.75)';
    g.lineWidth = 1.5;
    g.beginPath();
    for (let k = 0; k <= N; k++) {
      const t = a + ((b - a) * k) / N;
      const v = isIn && t < inStart ? 0 : gainAt(events, t);
      const px = X(t);
      if (k) g.lineTo(px, y(v)); else g.moveTo(px, y(v));
    }
    g.stroke();
    // Tag on the top lane: "NEXT · BASS SWAP · 8 BARS".
    if (this.which === 0) {
      g.font = `bold 10px ${MONO}`;
      this.tag(label, x0, 0, live ? COL.text : COL.text, live ? COL.onAir : COL.raised, 'left', true);
    }
    // Bass swap: the bar line the bass moves across, spanning the gap between the lanes.
    if (bassTo != null) {
      const bx = Math.round(X(bassTo.time));
      g.fillStyle = live ? COL.onAir : COL.text2;
      if (this.which === 0) g.fillRect(bx, H * 0.83, 2, H * 0.17);
      else {
        g.fillRect(bx, 0, 2, H * 0.17);
        g.font = `bold 10px ${MONO}`;
        this.tag(`BASS → ${bassTo.letter}`, bx - 4, 2, live ? COL.onAir : COL.text2, COL.bg, 'right', false, true);
      }
    }
  }

  /** Small mono label box. align: where x is (left edge, centre, right edge). */
  tag(text, x, y, fg, bg, align = 'left', square = false, outline = false) {
    const g = this.g;
    if (!square) g.font = `bold 10px ${MONO}`;
    const w = g.measureText(text).width + 12, h = 19;
    const left = align === 'center' ? x - w / 2 : align === 'right' ? x - w : x;
    g.fillStyle = bg;
    g.beginPath();
    g.roundRect(left, y, w, h, square ? 0 : 3);
    g.fill();
    if (outline) { g.strokeStyle = fg; g.lineWidth = 1; g.stroke(); }
    g.fillStyle = fg;
    g.textBaseline = 'middle';
    g.textAlign = 'left';
    g.fillText(text, left + 6, y + h / 2 + 0.5);
  }
}
