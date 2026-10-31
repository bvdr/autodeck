// node --test web/memory.test.mjs — mix memory (design xWVJy): the copy the pure helpers build.
import test from 'node:test';
import assert from 'node:assert/strict';
import { autoLabel, fxOptionLabel, isAvoided, isLoved, lovedLine, movedNote, pairOf, pairReason, pairSub, pairWhen } from './ui.js';

const loved = { effect: 'bass_swap', loved: 3, avoided: 0, why: null, last: null };

test('pair lookups are directional', () => {
  const pairs = { 'a>b': loved, 'b>c': { ...loved, loved: 0, avoided: 1, why: 'effect' } };
  assert.equal(pairOf(pairs, 'a', 'b'), loved);
  assert.equal(pairOf(pairs, 'b', 'a'), undefined);
  assert.equal(pairOf(pairs, null, 'b'), undefined);
  assert.ok(isLoved(loved) && !isLoved(pairs['b>c']) && !isLoved(undefined));
  // A wrong effect doesn't make the songs an avoided pair; no reason or "songs" does.
  assert.ok(!isAvoided(pairs['b>c']));
  assert.ok(isAvoided({ ...pairs['b>c'], why: null }) && isAvoided({ ...pairs['b>c'], why: 'songs' }));
});

test('Up next says why a song is there, or why one moved', () => {
  assert.equal(lovedLine('Despacito', loved), 'You loved it after Despacito · Bass swap · 3×');
  assert.equal(lovedLine('Despacito', { loved: 1, effect: null }), 'You loved it after Despacito');
  assert.equal(movedNote({ title: 'Dragostea Din Tei', by: 3, after_title: 'Tequila' }),
    'Dragostea Din Tei moved 3 later: you 👎 it right after Tequila.');
});

test('Pairs tab reasons and times', () => {
  const now = new Date(2026, 9, 31, 23, 0); // Sat 31 Oct, 23:00 local
  assert.equal(pairReason({ avoided: 1, why: null }), "songs don't go together");
  assert.equal(pairReason({ avoided: 1, why: 'songs', effect: 'echo' }), "songs don't go together");
  assert.equal(pairReason({ avoided: 1, why: 'effect', effect: 'echo' }), 'Echo out was wrong for them');
  assert.equal(pairWhen(new Date(2026, 9, 31, 21, 5).toISOString(), now), 'tonight');
  assert.equal(pairWhen(new Date(2026, 9, 30, 22, 0).toISOString(), now), 'last night');
  assert.equal(pairWhen(new Date(2026, 9, 25, 22, 14).toISOString(), now), 'last Sun, 22:14');
  assert.equal(pairWhen(new Date(2026, 9, 21).toISOString(), now), 'last week');
  assert.equal(pairWhen(new Date(2026, 9, 14).toISOString(), now), '2 weeks ago');
  assert.equal(pairWhen(new Date(2026, 8, 20).toISOString(), now), 'last month');
  assert.equal(pairWhen(new Date(2026, 5, 1).toISOString(), now), '5 months ago');
  assert.equal(pairWhen(null, now), ''); // migrated from the old format: no time known
  assert.equal(pairSub({ ...loved, last: new Date(2026, 9, 31, 22).toISOString() }, now), 'Bass swap · tonight');
  assert.equal(pairSub({ avoided: 1, why: null, effect: 'echo', last: null }, now), "songs don't go together");
});

test('the effect picker names the loved effect and dims the wrong one', () => {
  assert.equal(autoLabel('bass_swap', 'bass_swap'), 'Auto · Bass swap (you loved it)');
  assert.equal(autoLabel('bass_swap', 'bass_swap', true), 'Auto · Bass swap');
  assert.equal(autoLabel('blend', null), 'Auto · Beat blend');
  assert.equal(fxOptionLabel('echo', 'echo'), 'Echo out · you 👎 it here');
  assert.equal(fxOptionLabel('cut', 'echo'), 'Cut');
});
