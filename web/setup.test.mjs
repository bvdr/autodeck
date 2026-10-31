// node --test web/setup.test.mjs — headline texts of the first-launch setup window.
import test from 'node:test';
import assert from 'node:assert/strict';
import { describe } from './setup.js';

const st = (over) => ({ status: 'running', error: '', percent: 41.6, done_bytes: 412e6, total_bytes: 990e6, log: '',
  steps: [{ name: 'Python packages', status: 'running', detail: 'Downloading torch (75 MB)', percent: 41 },
          { name: 'AI models', status: 'waiting', detail: '', percent: 0 }], ...over });

test('shows the current action and bytes', () => {
  const d = describe(st());
  assert.equal(d.now, 'Downloading torch (75 MB)');
  assert.equal(d.amount, '412 MB of 990 MB · 41%');
});

test('outcomes win over the step text; percent is clamped', () => {
  assert.equal(describe(st({ status: 'failed' })).now, 'Setup stopped');
  assert.equal(describe(st({ status: 'cancelled' })).now, 'Cancelled');
  assert.match(describe(st({ status: 'done', percent: 100.4 })).now, /opening/);
  assert.equal(describe(st({ percent: 100.4 })).percent, 100);
});
