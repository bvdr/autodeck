// Pure helpers for setup.html (the standalone app's first-launch window). node --test web/setup.test.mjs

const MB = (b) => `${Math.round(b / 1e6).toLocaleString('en-US')} MB`;

/** Headline texts for a Setup progress snapshot (autodeck/standalone.py Progress.snapshot). */
export function describe(st) {
  const running = st.steps.find((s) => s.status === 'running');
  const now = st.status === 'done' ? 'Done, opening autodeck…'
    : st.status === 'failed' ? 'Setup stopped'
    : st.status === 'cancelled' ? 'Cancelled'
    : running ? (running.detail || running.name) : 'Starting…';
  return { percent: Math.min(100, Math.max(0, st.percent)), now, amount: `${MB(st.done_bytes)} of ${MB(st.total_bytes)} · ${Math.floor(st.percent)}%` };
}
