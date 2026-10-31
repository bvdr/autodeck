// node --test web/sources.test.mjs — the sources panel and the draft (pure helpers in ui.js).
import test from 'node:test';
import assert from 'node:assert/strict';
import { allReviewed, draftNote, upNextNote, pendingRows, sourceSelects, sourceSentence, sweepDraft, UNDO_MS } from './ui.js';

const mood = { kind: 'mood', name: 'Balkan party', key: 'mood:Balkan party', label: 'Balkan party' };
const hits = { kind: 'party', name: 'hits', key: 'party:Hits', label: 'Hits' };

test('the "playing from" sentence names the source', () => {
  assert.equal(sourceSentence(mood), 'Next songs come from the Balkan party mood.');
  assert.equal(sourceSentence({ kind: 'genre', name: 'Pop' }), 'Next songs come from the Pop genre.');
  assert.equal(sourceSentence(hits), 'Party is on: crowd-pleasers, energy building.');
  assert.match(sourceSentence(null), /whole library/);
});

test('only the source that is on is active; the other shows its default', () => {
  assert.deepEqual(sourceSelects({ genre: 'Balkan party', party: null }),
    { vibe: { value: 'Balkan party', active: true }, party: { value: 'Off', active: false }, playlist: { value: 'None', active: false } });
  assert.deepEqual(sourceSelects({ genre: 'Any', party: 'hits' }),
    { vibe: { value: 'Any', active: false }, party: { value: 'Hits', active: true }, playlist: { value: 'None', active: false } });
  assert.deepEqual(sourceSelects({ genre: 'Any', party: null, playlist: { name: 'Balkan brass' } }),
    { vibe: { value: 'Any', active: false }, party: { value: 'Off', active: false }, playlist: { value: 'Balkan brass', active: true } });
  const none = sourceSelects({ genre: 'Any', party: null });
  assert.equal(none.vibe.active || none.party.active, false);
});

test('draft line per source, and the no-source hint', () => {
  assert.equal(draftNote(null), 'Pick a mood, genre or party to get suggestions');
  assert.equal(draftNote(mood), 'Drafted from Balkan party. ✓ adds to Up next, ✕ never suggests it here again.');
  assert.equal(draftNote(hits), 'Drafted by Party: Hits. ✓ adds to Up next, ✕ never suggests it in Hits again.');
});

test('an excluded row keeps its Undo until the timer runs out, then leaves', () => {
  const t0 = 1000;
  const rows = [{ x: 1, state: 'new' }, { x: 2, state: 'excluded', until: t0 + UNDO_MS }, { x: 3, state: 'kept' }];
  assert.equal(sweepDraft(rows, t0 + UNDO_MS - 1).length, 3);
  assert.deepEqual(sweepDraft(rows, t0 + UNDO_MS).map((r) => r.x), [1, 3]);
  assert.deepEqual(pendingRows(rows).map((r) => r.x), [1]); // Add all skips kept and excluded
});

test('all reviewed once every row left is kept', () => {
  assert.equal(allReviewed([{ state: 'kept' }], 3), true);
  assert.equal(allReviewed([], 2), true); // all excluded
  assert.equal(allReviewed([{ state: 'kept' }, { state: 'excluded', until: 1 }], 2), false); // Undo still showing
  assert.equal(allReviewed([], 0), false); // nothing was drafted: not "reviewed"
});

test('Up next: kept rows say where they came from, auto picks say which source fills in', () => {
  const s = { source: mood, notice: null };
  assert.deepEqual(upNextNote({ kept: true, channel: 'Goran', kept_from: 'Balkan party' }, s),
    { auto: false, sub: 'Goran · kept from Balkan party' });
  assert.deepEqual(upNextNote({ kept: true, channel: 'Goran', kept_from: null }, s), { auto: false, sub: 'Goran' });
  assert.equal(upNextNote({ kept: false, channel: 'c' }, s).badge, 'Auto · Balkan party');
  assert.equal(upNextNote({ kept: false, channel: 'c' }, { source: null }).badge, 'Auto · whole library');
  assert.equal(upNextNote({ kept: false, channel: 'c' }, { source: mood, notice: 'No ready…' }).badge, 'Auto · any genre');
});
