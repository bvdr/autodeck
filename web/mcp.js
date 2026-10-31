// MCP · the AI DJ (design m6p10): the MCP button in the top bar, the card that drops under it when
// an assistant's command lands, and the MCP settings (on/off, steer / only read, Activity log, or
// how to connect one on first run). Reads only state.mcp; the "via MCP" tags live with the things
// they mark (ui.js, app.js).
import { api, el, icon, thumb, addPop, togglePop, report } from './ui.js';

const $ = (sel) => document.querySelector(sel);
const TOAST_MS = 5000;
const FLASH_MS = 700;

// --- pure (web/mcp.test.mjs) --------------------------------------------------------------------
/** "Claude Code" -> "Claude": only a short name fits next to the command in the button. */
export const shortName = (client) => (!client || /^an? /i.test(client) ? 'Assistant' : client.split(' ')[0]);

/** The button: off | working | error | on (a call in the last 10 min) | ready. `text` is what it
 * says after "MCP": the command in plain words while one runs, or the last error's reason. */
export function buttonState(m) {
  if (!m?.enabled) return { kind: 'off', text: null };
  // A command running now is newer news than an old error (which comes back once it's done).
  if (m.running) return { kind: 'working', text: `${shortName(m.running.client)} · ${m.running.text}` };
  if (m.last_error) return { kind: 'error', text: m.last_error };
  return { kind: m.connected ? 'on' : 'ready', text: null };
}

/** The settings' status pill. */
export function pill(m) {
  if (!m?.enabled) return { cls: 'off', text: 'Off' };
  if (m.mode === 'read') return { cls: 'read', text: 'Only read' };
  return m.running ? { cls: 'working', text: 'Working' } : { cls: 'ready', text: 'Ready' };
}

/** Reads (what's playing, library, search…) are many and change nothing: they fold into one
 * "Show N reads (…)" line. Steering calls and failed reads stay as rows. Log is newest first. */
export function foldReads(log) {
  const rows = [], names = [];
  let reads = 0;
  for (const x of log) {
    if (x.read && x.status !== 'error') {
      reads++;
      if (!names.includes(x.text)) names.push(x.text);
    } else rows.push(x);
  }
  return { rows, reads, names };
}
export const readsLine = (f) => `Show ${f.reads} read${f.reads === 1 ? '' : 's'} (${f.names.slice(0, 3).join(', ')}${f.names.length > 3 ? ', …' : ''})`;

/** Steering calls that finished since `seen` (ids already handled), newest first. */
export const landed = (log, seen) => log.filter((x) => !x.read && x.status !== 'running' && !seen.has(x.id));

// --- DOM --------------------------------------------------------------------------------------
const MODE_NOTE = {
  steer: 'Can queue songs, change vibe and party, pick effects, mix now, build moods, add songs.',
  read: 'Can see what’s playing, the library, history and suggestions. Anything that changes the session is refused with a clear reason.',
};
const hhmm = (at) => new Date(at * 1000).toTimeString().slice(0, 5);
const cap = (t) => t[0].toUpperCase() + t.slice(1);

let m = null;            // the last state.mcp
let seen = null;         // log ids already toasted (or there before the page loaded)
let showReads = false;   // the folded reads are open
let toastTimer = 0, flashTimer = 0;

function renderButton() {
  const b = $('#mcp-btn'), st = buttonState(m);
  b.dataset.kind = st.kind;
  $('#mcp-act').textContent = st.text ?? '';
  $('#mcp-act').hidden = !st.text;
  const what = { off: 'off, assistants can’t connect', ready: 'on, no assistant in the last 10 minutes',
    on: `${m?.client ?? 'an assistant'} connected`, working: st.text, error: `last command failed: ${st.text}` }[st.kind];
  b.setAttribute('aria-label', `MCP: ${what}. Open MCP settings`);
}

function entryRow(x) {
  const running = x.status === 'running';
  return el('li', { className: `mcp-entry ${x.status}` },
    el('time', { className: 'mono', textContent: hhmm(x.at) }),
    el('span', { className: 'what', textContent: running ? `${cap(x.text)}…` : x.text }),
    icon(running ? 'loader' : x.status === 'error' ? 'circle-alert' : 'check', running ? 'spin' : ''));
}

function renderPop() {
  if (!m || $('#mcp-pop').hidden) return;
  const p = pill(m);
  $('#mcp-pill').className = `mcp-pill ${p.cls}`;
  $('#mcp-pill span').textContent = p.text;
  $('#mcp-enable').setAttribute('aria-checked', String(m.enabled));
  $('#mcp-off-note').hidden = m.enabled;
  for (const b of document.querySelectorAll('#mcp-mode button')) {
    b.setAttribute('aria-pressed', String(b.dataset.mode === m.mode));
    b.disabled = !m.enabled; // Off: the choice is kept but means nothing until it's back on
  }
  $('#mcp-mode-note').textContent = MODE_NOTE[m.mode];
  $('.mcp-pop').classList.toggle('is-off', !m.enabled);
  // First run: until an assistant has ever called, show how to connect one instead of the log.
  $('#mcp-connect').hidden = !m.enabled || m.ever_connected;
  $('#mcp-activity').hidden = !m.enabled || !m.ever_connected;
  if ($('#mcp-activity').hidden) return;
  $('#mcp-client').textContent = m.client ? `${m.client} · ${m.connected ? 'connected' : `last seen ${hhmm(m.last_call_at)}`}` : '';
  const f = foldReads(m.log);
  $('#mcp-log').replaceChildren(...(showReads ? m.log : f.rows).map(entryRow));
  $('#mcp-empty').hidden = m.log.length > 0;
  $('#mcp-reads').hidden = !f.reads;
  $('#mcp-reads').textContent = showReads ? 'Hide reads' : readsLine(f);
}

function toast(x) {
  if (!$('#mcp-pop').hidden) return; // the open log shows it already
  const t = $('#mcp-toast');
  t.replaceChildren(
    el('p', { className: 'who' }, icon('bot'), `${x.client ?? 'An assistant'} · just now`),
    el('div', { className: 'body' }, x.song ? thumb({ id: x.song }) : null, el('p', { textContent: x.text })),
    el('button', { type: 'button', className: 'link-btn', textContent: 'Open log', onclick: () => togglePop('mcp', true) }));
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, TOAST_MS);
}

/** Every polled state (and every action's response). */
export function renderMcp(s) {
  const prev = m;
  m = s?.mcp ?? null;
  if (!m) return;
  // A read only flashes the dot: a new call that didn't make the button "working".
  if (prev && m.last_call_at !== prev.last_call_at && !m.running) {
    $('#mcp-btn').classList.add('flash');
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => $('#mcp-btn').classList.remove('flash'), FLASH_MS);
  }
  if (!seen) seen = new Set(m.log.map((x) => x.id)); // no toasts for what happened before the page
  const fresh = landed(m.log, seen);
  for (const x of fresh) seen.add(x.id);
  const ok = fresh.find((x) => x.status === 'ok'); // a failure shows on the button instead
  if (ok) toast(ok);
  // An error that arrives while the settings are open has been seen as it happened.
  if (m.last_error && !$('#mcp-pop').hidden) seenNow();
  renderButton();
  renderPop();
}

let clearing = false;
async function seenNow() {
  if (clearing) return; // one request at a time; polls keep coming meanwhile
  clearing = true;
  try { renderMcp({ mcp: (await api('/api/mcp/seen', 'POST')).mcp }); } catch (e) { report(e); } finally { clearing = false; }
}

async function save(body) {
  try { renderMcp({ mcp: (await api('/api/mcp/settings', 'PUT', body)).mcp }); } catch (e) { report(e); }
}

async function copy(btn, text) {
  try {
    await navigator.clipboard.writeText(text);
    btn.querySelector('use').setAttribute('href', '#i-check');
    setTimeout(() => btn.querySelector('use').setAttribute('href', '#i-copy'), 1500);
  } catch (e) { report(e); }
}

export function initMcp() {
  // The endpoint is this app's own address (autodeck serves /mcp next to the page).
  const url = `${location.origin}/mcp`;
  $('#mcp-url').textContent = url;
  $('#mcp-cmd').textContent = `claude mcp add --transport http --scope user autodeck ${url}`;
  addPop('mcp', { pop: '#mcp-pop', btns: ['#mcp-btn'], focus: '#mcp-enable', onOpen: () => {
    $('#mcp-toast').hidden = true;
    renderPop();
    // Opening the settings is how the owner says "seen": the red error clears.
    if (m?.last_error) seenNow();
  } });
  $('#mcp-btn').addEventListener('click', () => togglePop('mcp'));
  $('#mcp-enable').addEventListener('click', () => save({ enabled: !m?.enabled }));
  for (const b of document.querySelectorAll('#mcp-mode button')) b.addEventListener('click', () => save({ mode: b.dataset.mode }));
  $('#mcp-reads').addEventListener('click', () => { showReads = !showReads; renderPop(); });
  $('#mcp-copy-cmd').addEventListener('click', (e) => copy(e.currentTarget, $('#mcp-cmd').textContent));
  $('#mcp-copy-url').addEventListener('click', (e) => copy(e.currentTarget, url));
}
