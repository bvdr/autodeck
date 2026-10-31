// node --test web/settings.test.mjs — what Settings says about the Last.fm key (pure helpers in ui.js).
import test from 'node:test';
import assert from 'node:assert/strict';
import { browserNote, keyProblem, lastfmState, runtimeNote } from './ui.js';

test('key format is checked before any request (same rule as the server)', () => {
  assert.equal(keyProblem('0123456789abcdef0123456789ABCDEF'), null);
  assert.equal(keyProblem('  0123456789abcdef0123456789abcdef  '), null);
  assert.match(keyProblem(''), /Paste/);
  assert.match(keyProblem('nope'), /32 letters/);
  assert.match(keyProblem('g'.repeat(32)), /32 letters/);
});

test('state line shows only the last 4 characters and who set the key', () => {
  assert.deepEqual(lastfmState({ set: false, hint: null, source: null }),
    { text: 'Not set — party suggestions and Hits/Oldies/Latino need it', cls: 'unset', removable: false });
  const saved = lastfmState({ set: true, hint: '1e16', source: 'settings' });
  assert.equal(saved.text, 'Set · ends in …1e16');
  assert.ok(saved.removable);
  // Only a key saved from Settings can be removed from the page.
  const env = lastfmState({ set: true, hint: 'abcd', source: 'env' });
  assert.ok(!env.removable && /environment/.test(env.text));
  assert.ok(!lastfmState({ set: true, hint: 'abcd', source: 'legacy' }).removable);
});

test('YouTube login note: env var wins, none = public only, Safari needs Full Disk Access', () => {
  assert.match(browserNote({ browser: 'firefox', from_env: true }), /AUTODECK_BROWSER/);
  assert.match(browserNote({ browser: 'none', from_env: false }), /public/);
  assert.match(browserNote({ browser: 'safari', from_env: false }), /Full Disk Access/);
  assert.match(browserNote({ browser: 'brave', from_env: false }), /Brave login.*Keychain/);
});

test('Installation line: repair only in the standalone app', () => {
  assert.deepEqual(runtimeNote({ standalone: false, python: '3.12.9' }), { text: 'Running from the repo (Python 3.12.9)', repair: false });
  const r = runtimeNote({ standalone: true, python: '3.12.9', app_version: '0.1.0', size: 1.234e9 });
  assert.equal(r.text, 'autodeck 0.1.0 · Python 3.12.9 · 1.2 GB installed');
  assert.ok(r.repair);
});
