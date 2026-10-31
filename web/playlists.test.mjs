// node --test web/playlists.test.mjs — saved playlists (design HJGfo): the copy the pure helpers build.
import test from 'node:test';
import assert from 'node:assert/strict';
import { ago, failedNote, plCount, plMeta, plRowNote, removedNote, savedNote, sourceSentence, syncNote, upNextNote } from './ui.js';

const NOW = 1_000_000;
const p = { id: 'PL1', name: 'Balkan brass', count: 48, ready: 41, synced_at: NOW - 2 * 3600, left: 20 };

test('counts and the last sync', () => {
  assert.equal(ago(NOW - 30, NOW), 'just now');
  assert.equal(ago(NOW - 5 * 60, NOW), '5 min ago');
  assert.equal(ago(NOW - 2 * 3600, NOW), '2 h ago');
  assert.equal(ago(NOW - 3 * 86400, NOW), '3 d ago');
  assert.equal(plCount({ count: 48, ready: 12 }), '48 songs · 12 ready');
  assert.equal(plCount({ count: 86, ready: 86 }), '86 songs · all ready');
  assert.match(plMeta(p), /^48 songs · 41 ready · synced /);
  assert.equal(plMeta({ ...p, left: 2 }), '48 songs · 2 left'); // the end is near
});

test('playlist rows say where each song is (design 04/05)', () => {
  const decks = { of: (id) => (id === 'b' ? 'A' : null), next: () => 'B' };
  assert.equal(plRowNote({ pos: 1, state: 'played', played_at: '21:40' }, decks), '#1 · played 21:40');
  assert.equal(plRowNote({ id: 'b', pos: 2, state: 'playing' }, decks), '#2 · now playing on A');
  assert.equal(plRowNote({ pos: 3, state: 'next' }, decks), '#3 · next on deck B');
  assert.equal(plRowNote({ pos: 48, state: 'next', last: true }, decks), '#48 · next on deck B · last one');
  assert.equal(plRowNote({ pos: 4, state: '', artist: 'Goran Bregović' }, decks), '#4 · Goran Bregović');
  assert.equal(plRowNote({ pos: 5, state: 'waiting' }, decks), '#5 · downloading, skipped for now');
  assert.equal(plRowNote({ pos: 4, state: 'next', plan: 0, why: 'closest tempo' }, decks), '#4 → next · closest tempo');
  assert.equal(plRowNote({ pos: 7, state: '', plan: 1, why: 'same key' }, decks), '#7 → 3rd · same key');
  assert.equal(plRowNote({ pos: 3, state: '', plan: 2 }, decks), '#3 → 4th');
  assert.equal(plRowNote({ pos: 12, state: '' }, decks, true), '#12 · new on YouTube');
});

test('notices use the design copy', () => {
  assert.equal(syncNote({ added: [12, 30, 49], removed: 1 }),
    'Synced: 3 new songs added (#12, #30, #49). 1 was removed on YouTube: out of the playlist, still in your library.');
  assert.equal(syncNote({ added: [], removed: 0 }), 'Synced: nothing changed on YouTube.');
  assert.equal(savedNote({ playlist: p, found: 48, added: 31 }),
    'Balkan brass saved: 48 songs found, 31 new to the library. Downloading from the top.');
  assert.equal(removedNote({ name: 'Balkan brass', count: 48, was_playing: true }),
    'Removed Balkan brass. Its 48 songs stay in your library. Playing from Vibe: Any.');
  assert.equal(failedNote({ ...p, failed: 3, fail_reason: 'unavailable in your country' }),
    '3 songs in Balkan brass can’t play (unavailable in your country). They’re skipped; Retry is in Library.');
});

test('playing from a playlist: the sentence and Up next badges', () => {
  const src = { kind: 'playlist', name: 'Balkan brass', label: 'Balkan brass' };
  assert.equal(sourceSentence(src, { order: 'playlist' }), 'Playing the Balkan brass playlist, in order.');
  assert.equal(sourceSentence(src, { order: 'mix' }), 'Playing the Balkan brass playlist, in mix order.');
  assert.equal(upNextNote({ kept: false, in_playlist: true }, { source: src, notice: 'ends soon' }).badge, 'Auto · Balkan brass');
  assert.equal(upNextNote({ kept: false, in_playlist: false }, { source: src }).badge, 'Auto · library');
});
