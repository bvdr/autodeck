// node --test web/schedule.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import * as S from './schedule.js';

const close = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);
const res = (kind, length, out_at = 60, in_at = 2) => ({
  transition: { kind, length, out_id: 'a', in_id: 'b' }, out_at_rendered: out_at, in_at_rendered: in_at,
});
const ev = (events, param, type) => events.filter((e) => e.param === param && (!type || e.type === type));

test('T0 = outgoing zero + out_at_rendered; incoming starts at (T0, in_at_rendered)', () => {
  const t = S.timing(10, res('blend', 16));
  close(t.T0, 70);
  close(t.inStart, 70);
  close(t.inOffset, 2);
  close(t.inZero, 68);
  close(t.end, 86);
});

test('blend + half_double: equal-power curves over length, highpass 300->20 over first half', () => {
  for (const kind of ['blend', 'half_double']) {
    const p = S.plan(res(kind, 16), 10, 5);
    const [co] = ev(p.out, 'gain', 'curve'), [ci] = ev(p.in, 'gain', 'curve');
    assert.equal(co.time, 70); assert.equal(co.duration, 16);
    assert.equal(ci.time, 70); assert.equal(ci.duration, 16);
    close(co.values[0], 1); close(co.values.at(-1), 0, 1e-6);
    close(ci.values[0], 0); close(ci.values.at(-1), 1);
    for (let i = 0; i < co.values.length; i++) close(co.values[i] ** 2 + ci.values[i] ** 2, 1, 1e-5);
    const hp = ev(p.in, 'hp');
    assert.deepEqual(hp.at(-2), { param: 'hp', type: 'set', value: 300, time: 70 });
    assert.deepEqual(hp.at(-1), { param: 'hp', type: 'exp', value: 20, time: 78 });
  }
});

test('every touched param starts with a set at now (applier cancels from now)', () => {
  for (const kind of ['blend', 'half_double', 'cut', 'echo', 'dropswap', 'fade']) {
    const p = S.plan(res(kind, 8), 10, 5);
    for (const side of [p.out, p.in]) {
      for (const param of new Set(side.map((e) => e.param))) {
        const first = ev(side, param)[0];
        assert.equal(first.type, 'set', `${kind} ${param}`);
        assert.equal(first.time, 5, `${kind} ${param}`);
      }
      // AudioParam events must be time-ordered per param.
      for (const param of new Set(side.map((e) => e.param))) {
        const times = ev(side, param).map((e) => e.time);
        assert.deepEqual(times, [...times].sort((a, b) => a - b), `${kind} ${param} order`);
      }
    }
  }
});

test('cut: instant gain swap at T0, roles swap at T0', () => {
  const p = S.plan(res('cut', 0), 10, 5);
  assert.deepEqual(ev(p.out, 'gain').at(-1), { param: 'gain', type: 'set', value: 0, time: 70 });
  assert.equal(ev(p.in, 'gain').at(-1).value, 1);
  close(p.inStart, 70); close(p.end, 70);
});

test('dropswap: like cut, incoming offset at its drop, length ignored for the swap', () => {
  const p = S.plan(res('dropswap', 4, 60, 31.5), 10, 5);
  assert.deepEqual(ev(p.out, 'gain').at(-1), { param: 'gain', type: 'set', value: 0, time: 70 });
  close(p.inStart, 70); close(p.inOffset, 31.5); close(p.end, 70);
  assert.equal(S.faderAt(p, 69.9), 0);
  assert.equal(S.faderAt(p, 70), 1);
});

test('echo: one-beat delay, send on at T0, dry fades over length, incoming at T0+length', () => {
  const p = S.plan(res('echo', 4), 10, 5, 0.5);
  close(p.inStart, 74); close(p.inZero, 72); close(p.end, 74);
  assert.deepEqual(ev(p.out, 'delay')[0], { param: 'delay', type: 'set', value: 0.5, time: 5 });
  assert.deepEqual(ev(p.out, 'echo').at(-1), { param: 'echo', type: 'set', value: 1, time: 70 });
  assert.deepEqual(ev(p.out, 'gain').at(-1), { param: 'gain', type: 'linear', value: 0, time: 74 });
  assert.equal(S.ECHO_FEEDBACK, 0.5);
});

test('fade: linear crossfade over length', () => {
  const p = S.plan(res('fade', 4), 10, 5);
  assert.deepEqual(ev(p.out, 'gain').at(-1), { param: 'gain', type: 'linear', value: 0, time: 74 });
  assert.deepEqual(ev(p.in, 'gain').at(-1), { param: 'gain', type: 'linear', value: 1, time: 74 });
  close(S.faderAt(p, 72), 0.5);
});

test('crossfader follows the transition progress', () => {
  const p = S.plan(res('blend', 16), 10, 5);
  assert.equal(S.faderAt(p, 60), 0);
  close(S.faderAt(p, 74), 0.25);
  assert.equal(S.faderAt(p, 100), 1);
});

test('manual gains are equal-power across the fader', () => {
  for (const x of [0, 0.2, 0.5, 0.9, 1]) {
    const [a, b] = S.manualGains(x);
    close(a * a + b * b, 1);
  }
  assert.deepEqual(S.manualGains(0).map(Math.round), [1, 0]);
  assert.deepEqual(S.manualGains(1).map(Math.round), [0, 1]);
});

test('manual release: nearer deck wins if playing, else current keeps it', () => {
  assert.deepEqual(S.release(0.8, 0, [true, true]), { winner: 1, loser: 0 });
  assert.deepEqual(S.release(0.2, 0, [true, true]), { winner: 0, loser: 1 });
  assert.deepEqual(S.release(0.2, 1, [true, true]), { winner: 0, loser: 1 }); // pulled back
  assert.deepEqual(S.release(0.9, 0, [true, false]), { winner: 0, loser: 1 }); // B not started
  assert.deepEqual(S.release(0.5, 1, [true, true]), { winner: 1, loser: 0 }); // tie keeps cur
  const r = S.releaseRamps(10, 2, [0.6, 0.8], 1);
  assert.deepEqual(r[1].at(-1), { param: 'gain', type: 'linear', value: 1, time: 12 });
  assert.deepEqual(r[0].at(-1), { param: 'gain', type: 'linear', value: 0, time: 12 });
  assert.equal(r[0][0].value, 0.6);
});

test('replan guard: only before T0 - margin, only when the next song changed', () => {
  assert.ok(S.canReplan(10, 12));
  assert.ok(!S.canReplan(10.6, 12));
  assert.ok(!S.canReplan(13, 12));
  const base = { stateVersion: 5, planVersion: 4, planInId: 'b', nextId: 'c', now: 10, T0: 20 };
  assert.ok(S.shouldReplan(base));
  assert.ok(!S.shouldReplan({ ...base, stateVersion: 4 }));
  assert.ok(!S.shouldReplan({ ...base, nextId: 'b' }));
  assert.ok(!S.shouldReplan({ ...base, now: 19 }));   // inside the margin: keep the plan
  assert.ok(!S.shouldReplan({ ...base, now: 25 }));   // blend started: never refetch
  // "End of queue" plan (no incoming) refetches once a next song appears.
  assert.ok(S.shouldReplan({ ...base, planInId: null, T0: undefined }));
  assert.ok(!S.shouldReplan({ ...base, planInId: null, nextId: undefined, T0: undefined }));
});

test('tooSoon catches a plan whose T0 already passed while rendering', () => {
  assert.ok(S.tooSoon(10, 10.1));
  assert.ok(!S.tooSoon(12, 10));
});

test('drift check: aligned downbeats -> ~0 ms, a 20 ms offset is reported', () => {
  const bar = 2;
  const outDb = Array.from({ length: 50 }, (_, i) => i * bar);
  const inDb = Array.from({ length: 20 }, (_, i) => 2 + i * bar);
  const p = S.plan(res('blend', 16, 60, 2), 10, 5);
  close(S.driftMs(outDb, inDb, 10, p.inZero, p.T0, p.end), 0, 1e-3);
  close(S.driftMs(outDb, inDb, 10, p.inZero + 0.02, p.T0, p.end), 20, 1e-3);
  assert.equal(S.driftMs(outDb, [], 10, p.inZero, p.T0, p.end), null);
});

test('first offset is the trimmed play_start', () => {
  assert.equal(S.firstOffset({ sections_rendered: [{ start: 3.2 }], downbeats_rendered: [4] }), 3.2);
  assert.equal(S.firstOffset({ sections_rendered: [], downbeats_rendered: [4] }), 4);
  assert.equal(S.firstOffset({}), 0);
});

test('cancelling a plan: reset the outgoing before T0, hold it from T0 on (echo-out grab)', () => {
  const p = S.plan(res('echo', 4), 10, 5);
  assert.deepEqual(S.outgoingOnCancel(60, p.T0), [
    { param: 'gain', type: 'set', value: 1, time: 60 }, { param: 'echo', type: 'set', value: 0, time: 60 }]);
  assert.equal(S.outgoingOnCancel(p.T0, p.T0), null);
  assert.equal(S.outgoingOnCancel(p.T0 + 2, p.T0), null);
  assert.ok(S.KEY_RELEASE_MS >= 300);
});

// ui.js has no import-time DOM use, so its render gate is testable here with a stub
// document (kept in this file so `node --test web/schedule.test.mjs` stays the one command).
test('setState never re-renders under a focused select, even once dirty', async () => {
  let renders = 0;
  globalThis.document = {
    activeElement: { tagName: 'SELECT' },
    querySelector: () => { renders++; throw new Error('render'); },
  };
  const { setState } = await import('./ui.js');
  const st = (version) => ({ version, notice: null });
  setState(st(1)); // changed while the owner has the genre dropdown open: deferred
  assert.equal(renders, 0);
  setState(st(1)); // a poll with no change: still open, must not rebuild the DOM under it
  setState(st(2));
  assert.equal(renders, 0);
  globalThis.document.activeElement = null; // dropdown closed: the deferred render runs
  assert.throws(() => setState(st(2)), /render/);
  assert.ok(renders > 0);
  delete globalThis.document;
});
