// First-run onboarding: while the library is empty the page shows only this screen, which asks
// for a YouTube / YouTube Music playlist. It stays (with live prep progress) until the owner
// presses Start, which needs at least one ready song; it comes back whenever the library is
// emptied again. Pure parts are tested in onboard.test.mjs; the DOM part runs in the page.

export const LIKED_MUSIC = 'https://music.youtube.com/playlist?list=LM';

/** Which screen to show. `active` = onboarding is under way in this page (an empty library was
 * seen and Start wasn't pressed yet). Returns the next `active` too. */
export function gate(s, active) {
  const songs = s?.pool?.length ?? 0;
  if (songs === 0) return { screen: 'onboard', active: true }; // no way past an empty library
  return { screen: active ? 'onboard' : 'app', active };
}

/** Start is offered as soon as one song is ready; it names that song. */
export function startable(s) {
  const first = s?.pool?.find((p) => p.status === 'ready');
  return first ? { id: first.id, label: `Start with “${first.title}”` } : null;
}

/** "3 ready · 2 preparing · 25 waiting", from the pool. */
export function progressLine(s) {
  const n = { ready: 0, working: 0, queued: 0, failed: 0 };
  for (const p of s?.pool ?? []) {
    if (p.status === 'ready') n.ready++;
    else if (p.status === 'failed') n.failed++;
    else if (p.status === 'queued') n.queued++;
    else n.working++; // downloading / analyzing
  }
  return [`${n.ready} ready`, n.working && `${n.working} preparing`, n.queued && `${n.queued} waiting`,
    n.failed && `${n.failed} couldn't be added`].filter(Boolean).join(' · ');
}

/** What the browser note under Liked Music says. */
export function loginNote(browser) {
  return browser === 'none'
    ? 'Liked Music needs a YouTube login: pick the browser you use YouTube in.'
    : 'Liked Music is read with your YouTube login from this browser.';
}

// --- DOM -------------------------------------------------------------------------------
const $ = (sel) => document.querySelector(sel);
let active = false;
let found = null; // songs found by the last add, for the "Found N songs" line
let deps = null;

/** deps: { api, setOpener } from ui.js. */
export function initOnboard(d) {
  deps = d;
  const form = $('#ob-form');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const url = $('#ob-url').value.trim();
    const err = $('#ob-err');
    if (!url) { err.textContent = 'Paste a playlist link first.'; $('#ob-url').focus(); return; }
    err.textContent = '';
    const btn = form.querySelector('button[type="submit"]');
    btn.disabled = true;
    $('#ob-found').textContent = 'Reading the playlist…';
    try {
      const r = await deps.api('/api/add', 'POST', { url, genre: $('#ob-genre').value || null });
      found = r.added;
      $('#ob-found').textContent = found
        ? `Found ${found} song${found === 1 ? '' : 's'}, preparing the first ones…`
        : 'Those songs are already in your library.';
    } catch (x) {
      $('#ob-found').textContent = '';
      err.textContent = x.message; // the server's {error}: bad link, login problem, ...
      $('#ob-url').focus();
    } finally {
      btn.disabled = false;
    }
  });
  $('#ob-liked').addEventListener('click', () => { $('#ob-url').value = LIKED_MUSIC; $('#ob-url').focus(); });
  $('#ob-browser').addEventListener('change', async (e) => {
    try { await deps.api('/api/settings/youtube', 'PUT', { browser: e.target.value }); } catch (x) { $('#ob-err').textContent = x.message; }
    $('#ob-login').textContent = loginNote(e.target.value);
  });
  $('#ob-start').addEventListener('click', () => {
    const st = startable(deps.last());
    if (!st) return;
    active = false;
    deps.setOpener(st.id); // the PLAY lamp holds it
    show('app');
  });
}

function show(screen) {
  const on = screen === 'onboard';
  const was = !$('#onboard').hidden;
  $('#onboard').hidden = !on;
  document.querySelector('.app').hidden = on;
  document.body.classList.toggle('onboarding', on);
  if (on && !was) {
    loadBrowser();
    $('#ob-url').focus();
  }
}

async function loadBrowser() {
  try {
    const st = await deps.api('/api/settings');
    $('#ob-browser').value = st.youtube.browser;
    $('#ob-browser').disabled = st.youtube.from_env;
    $('#ob-login').textContent = loginNote(st.youtube.browser);
  } catch { /* the page's offline banner covers it */ }
}

/** Every state from the server. */
export function renderOnboard(s) {
  const g = gate(s, active);
  if (g.active && !active) found = null;
  active = g.active;
  show(g.screen);
  if (g.screen !== 'onboard') return;
  const genre = $('#ob-genre'), keep = genre.value;
  if (genre.options.length !== s.genres.length + 1) {
    genre.replaceChildren(new Option('auto-detect', ''), ...s.genres.map((x) => new Option(x, x)));
    genre.value = s.genres.includes(keep) ? keep : '';
  }
  const has = s.pool.length > 0;
  $('#ob-progress').hidden = !has;
  $('#ob-line').textContent = has ? progressLine(s) : '';
  const st = startable(s);
  $('#ob-start').hidden = !st;
  if (st) $('#ob-start-label').textContent = st.label;
}
