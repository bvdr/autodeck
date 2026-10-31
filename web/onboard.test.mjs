// node --test web/onboard.test.mjs — which screen shows: onboarding gates an empty library.
import test from 'node:test';
import assert from 'node:assert/strict';
import { gate, startable, progressLine, loginNote } from './onboard.js';

const song = (id, status, title = `Song ${id}`) => ({ id, status, title });

test('an empty library always shows onboarding, with no way past it', () => {
  assert.deepEqual(gate({ pool: [] }, false), { screen: 'onboard', active: true });
  assert.deepEqual(gate({ pool: [] }, true), { screen: 'onboard', active: true });
  assert.equal(gate(null, false).screen, 'onboard'); // before the first state: nothing to show yet
});

test('a library that has songs opens the app, unless onboarding is under way', () => {
  assert.deepEqual(gate({ pool: [song('a', 'ready')] }, false), { screen: 'app', active: false });
  // Just added a playlist: stay on the progress screen until Start is pressed.
  assert.deepEqual(gate({ pool: [song('a', 'queued')] }, true), { screen: 'onboard', active: true });
});

test('library emptied later: onboarding comes back', () => {
  let g = gate({ pool: [song('a', 'ready')] }, false);
  g = gate({ pool: [] }, g.active);
  assert.deepEqual(g, { screen: 'onboard', active: true });
});

test('Start needs a ready song and names the first one', () => {
  assert.equal(startable({ pool: [song('a', 'downloading'), song('b', 'queued')] }), null);
  assert.deepEqual(startable({ pool: [song('a', 'analyzing'), song('b', 'ready', 'Africa')] }),
    { id: 'b', label: 'Start with “Africa”' });
});

test('progress line counts ready / preparing / waiting / failed', () => {
  const s = { pool: [song('a', 'ready'), song('b', 'downloading'), song('c', 'analyzing'), song('d', 'queued'), song('e', 'failed')] };
  assert.equal(progressLine(s), "1 ready · 2 preparing · 1 waiting · 1 couldn't be added");
  assert.equal(progressLine({ pool: [song('a', 'queued')] }), '0 ready · 1 waiting');
});

test('Liked Music note depends on the login browser', () => {
  assert.match(loginNote('none'), /needs a YouTube login/);
  assert.match(loginNote('chrome'), /YouTube login/);
});
