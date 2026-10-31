// node --test web/mcp.test.mjs — the MCP button's state and the Activity log's folded reads (design m6p10).
import test from 'node:test';
import assert from 'node:assert/strict';
import { buttonState, pill, foldReads, readsLine, landed, shortName } from './mcp.js';

const m = (o = {}) => ({ enabled: true, mode: 'steer', client: null, connected: false, running: null, last_error: null, ...o });

test('the button has five states, read from state.mcp only', () => {
  assert.equal(buttonState(m({ enabled: false })).kind, 'off');
  assert.equal(buttonState(null).kind, 'off'); // before the first state
  assert.deepEqual(buttonState(m()), { kind: 'ready', text: null });
  assert.deepEqual(buttonState(m({ connected: true, client: 'Claude Code' })), { kind: 'on', text: null });
  // Working names the command in plain words with the client's short name, never a tool name.
  assert.deepEqual(buttonState(m({ connected: true, running: { tool: 'play_next', text: 'playing next', client: 'Claude Code' } })),
    { kind: 'working', text: 'Claude · playing next' });
  assert.deepEqual(buttonState(m({ connected: true, last_error: 'Couldn’t find that song' })),
    { kind: 'error', text: 'Couldn’t find that song' });
  // A command running now beats an old error; Off beats everything.
  assert.equal(buttonState(m({ last_error: 'x', running: { text: 'mixing now', client: 'Cursor' } })).kind, 'working');
  assert.equal(buttonState(m({ enabled: false, last_error: 'x' })).kind, 'off');
});

test('short client names', () => {
  assert.equal(shortName('Claude Code'), 'Claude');
  assert.equal(shortName('An assistant'), 'Assistant');
  assert.equal(shortName(null), 'Assistant');
});

test('the settings pill', () => {
  assert.deepEqual(pill(m({ enabled: false })), { cls: 'off', text: 'Off' });
  assert.deepEqual(pill(m({ mode: 'read' })), { cls: 'read', text: 'Only read' });
  assert.deepEqual(pill(m({ running: { text: 'x' } })), { cls: 'working', text: 'Working' });
  assert.deepEqual(pill(m()), { cls: 'ready', text: 'Ready' });
});

const e = (id, read, status = 'ok', text = id) => ({ id, read, status, text });

test('reads fold into one line; steering calls and failed reads stay in the log', () => {
  const log = [e('s2', false), e('now playing', true), e('r-err', true, 'error', 'YouTube search failed'),
    e('library', true), e('now playing', true), e('s1', false, 'running'), e('search', true), e('history', true)];
  const f = foldReads(log);
  assert.deepEqual(f.rows.map((x) => x.text), ['s2', 'YouTube search failed', 's1']);
  assert.equal(f.reads, 5);
  assert.deepEqual(f.names, ['now playing', 'library', 'search', 'history']);
  assert.equal(readsLine(f), 'Show 5 reads (now playing, library, search, …)');
  assert.equal(readsLine(foldReads([e('a', true)])), 'Show 1 read (a)');
  assert.equal(foldReads([e('x', false)]).reads, 0);
});

test('a toast is for a steering call that just landed, once', () => {
  const seen = new Set(['old']);
  const log = [e('new', false), e('read', true), e('busy', false, 'running'), e('bad', false, 'error'), e('old', false)];
  assert.deepEqual(landed(log, seen).map((x) => x.id), ['new', 'bad']);
});
