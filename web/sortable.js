// Drag-to-reorder for a list, with pointer events (mouse, touch and pen alike), a placeholder
// gap showing where the row will land, and auto-scroll near the window edges.
// Replaces HTML5 drag-and-drop, which has no touch support and no real drop preview.

/** Index the dragged row lands at: how many other rows' midpoints are above the pointer.
 * Pure, so node tests can check it. `mids` are the other rows' vertical midpoints, in order. */
export function dropIndex(mids, y) {
  let i = 0;
  for (const m of mids) if (y > m) i++;
  return i;
}

const EDGE = 60;   // px from the window edge where auto-scroll starts
const SPEED = 12;  // px per pointer move while in that zone

/**
 * Make `list`'s rows (li[data-id]) draggable by their `.grip`.
 * onStart(): a drag began (defer re-renders). onEnd(): it ended, dropped or cancelled.
 * onDrop(id, index): index among the OTHER rows, i.e. ready for "remove, then insert at".
 * onMove(gap, index): the placeholder moved to `index` (e.g. to label it "plays 4th").
 */
export function sortable(list, { onStart = () => {}, onEnd = () => {}, onDrop, onMove = () => {} }) {
  list.addEventListener('pointerdown', (e) => {
    const grip = e.target.closest('.grip');
    const li = grip?.closest('li[data-id]');
    if (!li || e.button > 0) return;
    e.preventDefault(); // no text selection, no touch scrolling while dragging
    const box = li.getBoundingClientRect(), dy = e.clientY - box.top;
    const others = [...list.children].filter((r) => r !== li && r.matches('li[data-id]'));
    const gap = document.createElement('li');
    gap.className = 'row placeholder';
    gap.style.height = `${box.height}px`;
    li.after(gap);
    const after = others.indexOf(gap.nextElementSibling);
    onMove(gap, after >= 0 ? after : others.length);
    // The row floats under the pointer; the gap holds its place in the list.
    Object.assign(li.style, { position: 'fixed', left: `${box.left}px`, top: `${box.top}px`,
      width: `${box.width}px`, zIndex: 20, pointerEvents: 'none' });
    li.classList.add('lifted');
    grip.setPointerCapture(e.pointerId);
    onStart();

    const move = (ev) => {
      li.style.top = `${ev.clientY - dy}px`;
      if (ev.clientY < EDGE) window.scrollBy(0, -SPEED);
      else if (ev.clientY > window.innerHeight - EDGE) window.scrollBy(0, SPEED);
      const mids = others.map((r) => { const b = r.getBoundingClientRect(); return b.top + b.height / 2; });
      const idx = dropIndex(mids, ev.clientY);
      list.insertBefore(gap, others[idx] ?? null);
      onMove(gap, idx);
    };
    const finish = (drop) => {
      grip.removeEventListener('pointermove', move);
      grip.removeEventListener('pointerup', up);
      grip.removeEventListener('pointercancel', cancel);
      const index = [...list.children].filter((r) => r !== li && r.matches('li[data-id], .placeholder')).indexOf(gap);
      li.removeAttribute('style');
      li.classList.remove('lifted');
      gap.replaceWith(li);
      onEnd();
      if (drop) onDrop(li.dataset.id, index);
    };
    const up = () => finish(true);
    const cancel = () => finish(false);
    grip.addEventListener('pointermove', move);
    grip.addEventListener('pointerup', up);
    grip.addEventListener('pointercancel', cancel);
  });
}
