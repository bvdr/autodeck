// node --test web/sortable.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { dropIndex } from './sortable.js';

test('drop index counts the other rows whose middle is above the pointer', () => {
  const mids = [20, 60, 100]; // three other rows
  assert.equal(dropIndex(mids, 5), 0);    // above everything -> first
  assert.equal(dropIndex(mids, 61), 2);   // just past the 2nd row's middle
  assert.equal(dropIndex(mids, 500), 3);  // below everything -> last
  assert.equal(dropIndex([], 50), 0);
});
