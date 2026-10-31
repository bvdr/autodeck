// node --test web/decks.test.mjs — "Play next on deck B" (design yA49d): which deck the pick lands on, and its notice.
import test from 'node:test';
import assert from 'node:assert/strict';
import { pickDeck, REPLAN_MARGIN } from './schedule.js';
import { pickNote, sheetActions } from './ui.js';

test('the pick lands on the idle deck, or after a running mix on the deck that frees up', () => {
  assert.deepEqual(pickDeck({ live: false, cur: 1, plan: null, now: 0 }), { deck: 0, afterMix: false }); // Play starts on A
  assert.deepEqual(pickDeck({ live: true, cur: 0, plan: null, now: 10 }), { deck: 1, afterMix: false });
  // A mix planned but not audible yet gets replanned: still the idle deck.
  assert.deepEqual(pickDeck({ live: true, cur: 0, plan: { T0: 60, inDeck: 1 }, now: 10 }), { deck: 1, afterMix: false });
  // Inside the replan margin or mixing: both decks are busy, the outgoing one frees up when it ends.
  assert.deepEqual(pickDeck({ live: true, cur: 0, plan: { T0: 60, inDeck: 1 }, now: 60 - REPLAN_MARGIN }), { deck: 0, afterMix: true });
  assert.deepEqual(pickDeck({ live: true, cur: 1, plan: { T0: 60, inDeck: 0 }, now: 70 }), { deck: 1, afterMix: true });
});

test('one line says what changed', () => {
  const r = { title: 'Smooth Criminal', deck: 'B' };
  assert.equal(pickNote({ ...r, ready: true, planned: 'Despacito' }),
    'Smooth Criminal is next on deck B. Despacito moved back to the top of Up next.');
  assert.equal(pickNote({ ...r, ready: true, planned: null }), 'Smooth Criminal is next on deck B.');
  assert.equal(pickNote({ title: 'Mi Gente', deck: 'B', ready: false, planned: 'Despacito' }),
    'Mi Gente goes on deck B once it’s analysed. Until then Despacito stays planned.');
  assert.equal(pickNote({ title: 'Mi Gente', deck: 'B', ready: false, planned: null }), 'Mi Gente goes on deck B once it’s analysed.');
  assert.equal(pickNote({ title: 'Tequila', deck: 'A', afterMix: true, ready: true, planned: 'Despacito' }),
    'Tequila plays after this mix, on deck A.');
});

test('the phone sheet offers only what applies to the list', () => {
  const ids = (a) => a.map((x) => x.id);
  assert.deepEqual(ids(sheetActions('queue', { i: 3, locked: 2, kept: false })), ['next', 'top', 'pin', 'remove']);
  assert.deepEqual(ids(sheetActions('queue', { i: 2, locked: 2, kept: true })), ['next', 'remove']); // at the top already, pinned already
  assert.deepEqual(ids(sheetActions('queue', { i: 0, locked: 2, kept: false })), ['remove']); // already next, locked in
  assert.deepEqual(ids(sheetActions('played', {})), ['next']);
  assert.deepEqual(ids(sheetActions('library', {})), ['next']);
});
