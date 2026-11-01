// node --test web/sections.test.mjs — "Loop a section": section spans, loop timing, the re-planned
// exit after a loop, and when section clicks are allowed.
import test from 'node:test';
import assert from 'node:assert/strict';
import { sectionSpan, makeLoop, exitLoop, loopPos, loopPass, loopZero, loopDoneAt, lastPassAt, looping, loopCount,
  exitAfterLoop, sectionsOpen, loopable, timing, REPLAN_MARGIN } from './schedule.js';

const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);
// 120 BPM: a bar is 2 s. Rendered downbeats every bar from 0.5 s; sections as the server sends them
// (sections_rendered = analysis sections through the render's time map).
const db = Array.from({ length: 64 }, (_, k) => 0.5 + 2 * k);
const info = { downbeats_rendered: db, sections_rendered: [
  { start: 0.5, end: 16.5, label: 'intro' }, { start: 16.5, end: 64.5, label: 'body' },
  { start: 64.5, end: 96.5, label: 'body' }, { start: 96.5, end: 127.9, label: 'outro' }] };

test('section bounds in rendered time: snapped to downbeats, with bar numbers', () => {
  assert.deepEqual(sectionSpan(info, 2), { start: 64.5, end: 96.5, bars: [33, 48] });
  assert.deepEqual(sectionSpan(info, 0).bars, [1, 8]);
  // The outro ends at play_end, off the grid: the loop ends on the nearest bar line instead.
  assert.deepEqual(sectionSpan(info, 3), { start: 96.5, end: 126.5, bars: [49, 63] });
  // A tempo-eased render: section edges are where the time map put them, a few ms off the grid.
  const eased = { downbeats_rendered: [0, 1.9, 3.85, 5.8], sections_rendered: [{ start: 1.903, end: 5.797, label: 'body' }] };
  assert.deepEqual(sectionSpan(eased, 0), { start: 1.9, end: 5.8, bars: [2, 3] });
  // No grid: the raw bounds.
  assert.deepEqual(sectionSpan({ downbeats_rendered: [], sections_rendered: [{ start: 3, end: 9, label: 'body' }] }, 0),
    { start: 3, end: 9, bars: null });
});

test('×N: the section plays N times, then the song carries on (N-1) section lengths later', () => {
  const sp = sectionSpan(info, 2), L = 32;
  // Deck zero at ctx 100, playhead at rendered 70 (inside bars 33–48) when ×2 is picked.
  const l = makeLoop(sp, 2, 170, 70);
  assert.equal(l.n, 2);
  close(loopPos(l, 170 + 26.4), 96.4);  // just before the end of pass 1
  close(loopPos(l, 170 + 26.6), 64.6);  // jumped back to the start
  assert.equal(loopPass(l, 170 + 26.6), 2);
  close(loopDoneAt(l), 100 + 96.5 + L); // the loop end time for ×2
  close(loopPos(l, loopDoneAt(l) + 1), 97.5); // then plain playback, shifted by one pass
  close(loopZero(l), 100 + L);
  close(lastPassAt(l), 100 + 96.5);     // the source stops looping during this pass
  assert.equal(loopCount(l, 170 + 30), '2 of 2');
  assert.ok(looping(l, loopDoneAt(l) - 0.1) && !looping(l, loopDoneAt(l)));
  // ×8 picked for a section still ahead: done when it has played 8 times.
  const ahead = makeLoop(sp, 8, 130, 30);
  close(loopDoneAt(ahead), 100 + 96.5 + 7 * L);
  assert.equal(loopCount(ahead, 130), '1 of 8');
});

test('changing ×N counts the passes already played; Exit loop finishes this pass', () => {
  const sp = sectionSpan(info, 2), L = 32;
  const l = makeLoop(sp, 4, 170, 70);           // ×4, pass 1 at ctx 170
  const t = 170 + 26.5 + L + 5;                 // in pass 3
  assert.equal(loopCount(l, t), '3 of 4');
  const two = makeLoop(sp, 2, t, loopPos(l, t), l); // ×2 now: this pass is the last one
  assert.equal(two.n, 1);
  assert.equal(loopCount(two, t), '3 of 3');
  const eight = makeLoop(sp, 8, t, loopPos(l, t), l);
  assert.equal(loopCount(eight, t), '3 of 8');
  close(loopDoneAt(eight), loopDoneAt(l) + 4 * L);
  // Exit loop in pass 3 of 4: pass 3 is the last, the song goes on one pass earlier.
  const ex = exitLoop(l, t);
  assert.equal(ex.n, 1);
  close(loopDoneAt(ex), loopDoneAt(l) - L);
  close(loopZero(ex), loopZero(l) - L);
  // Until I stop, exited right before a wrap: too close to trust, one more pass.
  const inf = makeLoop(sp, Infinity, 170, 70);
  assert.equal(loopCount(inf, 400), '∞');
  assert.ok(looping(inf, 1e9));
  const late = exitLoop(inf, 170 + 26.4);
  assert.equal(late.n, 2);
  close(loopDoneAt(late), 170 + 26.5 + L);
});

test('the re-planned exit lands after the loop, on the planner’s boundary, in ctx time', () => {
  const sp = sectionSpan(info, 2), L = 32;
  const l = makeLoop(sp, 4, 170, 70);
  // The planner's usual exit (outro start, 96.5) is the loop's end: fine, and T0 moves 3 passes later.
  assert.equal(exitAfterLoop(l, 96.5), null);
  const res = { transition: { kind: 'blend', length: 16 }, out_at_rendered: 112.5, in_at_rendered: 0 };
  close(timing(loopZero(l), res).T0, 100 + 112.5 + 3 * L);
  close(timing(loopZero(l), res).T0, loopDoneAt(l) + 16); // = 8 bars after the loop: still on the grid
  // An exit inside or before the loop can't be scheduled on the after-loop timeline: ask after it.
  assert.equal(exitAfterLoop(l, 80.5), 96.5);
  assert.equal(exitAfterLoop(l, 32.5), 96.5);
  assert.equal(exitAfterLoop(null, 32.5), null);
});

test('section clicks: only while the mix can still change; ON AIR only what’s still ahead', () => {
  const plan = { T0: 100 };
  assert.ok(sectionsOpen({ manual: false, plan: null, now: 50 }));
  assert.ok(sectionsOpen({ manual: false, plan, now: 100 - REPLAN_MARGIN - 0.01 }));
  assert.ok(!sectionsOpen({ manual: false, plan, now: 100 - REPLAN_MARGIN })); // locked
  assert.ok(!sectionsOpen({ manual: false, plan, now: 105 }));                 // mixing
  assert.ok(!sectionsOpen({ manual: true, plan: null, now: 50 }));             // owner on the fader
  const sp = sectionSpan(info, 2);
  assert.ok(loopable(sp, 30) && loopable(sp, 90));
  assert.ok(!loopable(sp, 96.4) && !loopable(sp, 100)); // too close to its end / already played
});
