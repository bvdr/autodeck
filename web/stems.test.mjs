// Voice / Beat switch and Vocal swap (design "Voice & beat (stems)"): the pure parts in schedule.js.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { modeGains, swapEvents, plan, stemsOpen, STEM_FADE, REPLAN_MARGIN } from './schedule.js';

test('modes pick one source: the plain song, its vocals or its instrumental', () => {
  assert.deepEqual(modeGains('both'), { main: 1, voc: 0, inst: 0 });
  assert.deepEqual(modeGains('voice'), { main: 0, voc: 1, inst: 0 });
  assert.deepEqual(modeGains('beat'), { main: 0, voc: 0, inst: 1 });
  assert.ok(STEM_FADE > 0 && STEM_FADE <= 0.05); // a few ms: no click, still instant
});

const res = (kind) => ({ transition: { kind, length: 8 }, out_at_rendered: 100, in_at_rendered: 0 });

test('vocal swap: a beat blend whose outgoing drops to its instrumental over the first beat', () => {
  const blend = plan(res('blend'), 0, 50, 0.5), swap = plan(res('vocal_swap'), 0, 50, 0.5);
  // Same crossfade and incoming as a blend.
  assert.deepEqual(swap.in, blend.in);
  assert.deepEqual(swap.out.filter((e) => e.param === 'gain'), blend.out.filter((e) => e.param === 'gain'));
  const at = (k, type, time) => swap.out.find((e) => e.param === k && e.type === type && e.time === time);
  // Until T0 (100) A plays as it is (Both), then main+vocals -> 0 and instrumental -> 1 by T0 + beat.
  assert.equal(at('main', 'set', 50).value, 1);
  assert.equal(at('inst', 'set', 100).value, 0);
  assert.equal(at('main', 'linear', 100.5).value, 0);
  assert.equal(at('voc', 'linear', 100.5).value, 0);
  assert.equal(at('inst', 'linear', 100.5).value, 1);
  // Other effects never touch the stem gains (an owner's Voice/Beat choice survives them).
  for (const k of ['blend', 'bass_swap', 'echo', 'cut']) assert.ok(!plan(res(k), 0, 50, 0.5).out.some((e) => ['main', 'voc', 'inst'].includes(e.param)));
});

test('vocal swap starts from the mode the owner left A in', () => {
  const ev = swapEvents('voice', 100, 0.5);
  assert.deepEqual(ev.filter((e) => e.type === 'set').map((e) => [e.param, e.value]), [['main', 0], ['voc', 1], ['inst', 0]]);
  assert.ok(ev.every((e) => e.time >= 100)); // re-applied from T0, so it never cuts into a fade happening now
  const p = plan(res('vocal_swap'), 0, 50, 0.5, 'beat');
  assert.equal(p.out.find((e) => e.param === 'inst' && e.time === 50).value, 1);
});

test('the switch: stems needed for Voice/Beat, locked during a mix like everything else', () => {
  const T0 = 100;
  assert.deepEqual(stemsOpen({ manual: false, plan: null, now: 0, stems: 'ready' }), { open: true, why: null });
  assert.equal(stemsOpen({ manual: false, plan: null, now: 0, stems: 'preparing' }).why, 'Preparing stems…');
  assert.equal(stemsOpen({ manual: false, plan: null, now: 0, stems: 'failed' }).why, 'No stems · split failed');
  assert.equal(stemsOpen({ manual: false, plan: null, now: 0, stems: undefined }).why, 'No stems for this song');
  const locked = { open: false, why: 'Locked during the mix' };
  assert.deepEqual(stemsOpen({ manual: false, plan: { T0 }, now: T0 - REPLAN_MARGIN + 0.1, stems: 'ready' }), locked);
  assert.deepEqual(stemsOpen({ manual: false, plan: { T0 }, now: T0 + 2, stems: 'ready' }), locked);
  assert.deepEqual(stemsOpen({ manual: true, plan: null, now: 0, stems: 'ready' }), locked);
  assert.equal(stemsOpen({ manual: false, plan: { T0 }, now: T0 - 10, stems: 'ready' }).open, true); // planned, not yet locked
});
