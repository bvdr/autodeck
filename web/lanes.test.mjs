// node --test web/lanes.test.mjs — lane geometry for the timeline (pure functions only).
import test from 'node:test';
import assert from 'node:assert/strict';
import { laneLeft, xAt, gainAt, mixBars, mixProgress, overview, mixesWith, mixTag, incomingStart,
  VIEW_SPAN, NOW_AT, MIX_AT, NOW_MAX, mixBeat, mixAnnouncement } from './lanes.js';
import { FX_LABEL, FX_SHAPE } from './ui.js';
import { plan, bassMid, BASS_CUT, REPLAN_MARGIN } from './schedule.js';

const close = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

test('NOW sits at 10 % with nothing planned or a mix far ahead', () => {
  close(xAt(100, laneLeft(100), 1000), NOW_AT * 1000);
  close(xAt(100, laneLeft(100, 100 + VIEW_SPAN), 1000), NOW_AT * 1000);
});

test('a running mix stays in view: window at 25 %, NOW walks through it', () => {
  const T0 = 100, left = laneLeft(105, T0);
  close(xAt(T0, left, 1), MIX_AT);
  assert.ok(xAt(105, left, 1) > MIX_AT);
  // Long after T0 the view follows NOW again, NOW never past NOW_MAX.
  close(xAt(500, laneLeft(500, T0), 1), NOW_MAX);
});

test('gainAt follows set / linear / curve events like an AudioParam', () => {
  const ev = [{ param: 'gain', type: 'set', value: 1, time: 0 }, { param: 'hp', type: 'set', value: 5, time: 0 },
    { param: 'gain', type: 'set', value: 1, time: 10 }, { param: 'gain', type: 'linear', value: 0, time: 20 }];
  close(gainAt(ev, 5), 1);
  close(gainAt(ev, 15), 0.5);
  close(gainAt(ev, 25), 0);
  const curve = [{ param: 'gain', type: 'set', value: 0, time: 0 },
    { param: 'gain', type: 'curve', values: Float32Array.from([0, 1]), time: 10, duration: 10 }];
  close(gainAt(curve, 9), 0);
  close(gainAt(curve, 15), 0.5);
  close(gainAt(curve, 30), 1);
});

test('drawn curves match the planned blend: equal power at the midpoint', () => {
  const res = { transition: { kind: 'blend', length: 8 }, out_at_rendered: 50, in_at_rendered: 2 };
  const p = plan(res, 0, 10, 0.5);
  close(gainAt(p.out, 49), 1);
  close(gainAt(p.out, 54) ** 2 + gainAt(p.in, 54) ** 2, 1, 1e-3);
  close(gainAt(p.in, 58), 1);
});

test('bass swap marker sits where the plan moves the highpass', () => {
  const beat = 0.5, bar = 2;
  const p = plan({ transition: { kind: 'bass_swap', length: 16 }, out_at_rendered: 40, in_at_rendered: 0 }, 0, 1, beat);
  const mid = bassMid(p.T0, p.length, bar);
  assert.ok(p.out.some((e) => e.param === 'hp' && e.value === BASS_CUT && e.time === mid));
  close(mid, 48);
});

test('bars, progress and tags for the sign and the window', () => {
  assert.equal(mixBars(19.2, 2.4), 8);
  assert.equal(mixBars(0, 2.4), 0);
  assert.deepEqual(mixProgress(110, 100, 19.2, 2.4), { bar: 5, bars: 8, left: 9.2 });
  assert.equal(mixProgress(200, 100, 19.2, 2.4).bar, 8);
  assert.equal(mixTag('bass_swap', 'Bass swap', 19.2, 2.4, false), 'NEXT · BASS SWAP · 8 BARS');
  assert.equal(mixTag('cut', 'Cut', 0, 2.4, true), 'CUT');
  assert.equal(incomingStart({ kind: 'echo', T0: 10, length: 4 }), 14);
  assert.equal(incomingStart({ kind: 'blend', T0: 10, length: 4 }), 10);
});

test('overview strip and the mixes check', () => {
  assert.deepEqual(overview([{ start: 0, end: 10, label: 'intro' }, { start: 10, end: 90, label: 'body' },
    { start: 90, end: 100, label: 'outro' }], 100).map((s) => [s.label, s.from, s.to]),
  [['intro', 0, 0.1], ['main', 0.1, 0.9], ['outro', 0.9, 1]]);
  assert.deepEqual(overview([], 100), []);
  assert.ok(mixesWith(120, 124) && mixesWith(120, 61) && !mixesWith(120, 135) && !mixesWith(120, null));
});

test('mixBeat: planned -> locked at the replan margin -> mixing at T0 -> handover at the midpoint', () => {
  const p = { T0: 100, length: 16 };
  assert.equal(mixBeat(50, null), null);
  assert.equal(mixBeat(100 - REPLAN_MARGIN - 0.01, p), 'planned');
  assert.equal(mixBeat(100 - REPLAN_MARGIN, p), 'locked');
  assert.equal(mixBeat(100, p), 'mixing');
  assert.equal(mixBeat(107.99, p), 'mixing');
  assert.equal(mixBeat(108, p), 'handover');
  // An instant swap (cut) has no mixing beat: it hands over on the downbeat.
  assert.equal(mixBeat(100, { T0: 100, length: 0 }), 'handover');
});

test('mixAnnouncement: one line per beat, as the board words it', () => {
  assert.equal(mixAnnouncement('mixing', 'Walking The Wire', 'Bass swap'), 'Mixing into Walking The Wire, bass swap');
  assert.equal(mixAnnouncement('planned', 'Walking The Wire', 'Bass swap'), 'Next mix: Walking The Wire, bass swap');
  assert.equal(mixAnnouncement('locked', 'Walking The Wire', 'Bass swap'), 'Bass swap locked for Walking The Wire');
  assert.equal(mixAnnouncement('handover', 'Walking The Wire'), 'Walking The Wire is on air');
  assert.equal(mixAnnouncement('settle', 'Walking The Wire'), 'Mix done. Now playing Walking The Wire');
  assert.equal(mixAnnouncement(null, 'x'), '');
});

test('every effect has a shape thumbnail with an A-out and a B-in curve', () => {
  for (const k of Object.keys(FX_LABEL)) {
    assert.ok(FX_SHAPE[k]?.a && FX_SHAPE[k]?.b, `no shape for ${k}`);
  }
});
