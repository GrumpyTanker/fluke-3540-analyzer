// Multi-session state container. Each loaded file (binary / .fel / .csv)
// becomes one entry. Single-session view uses the "active" entry;
// compare mode iterates the whole list.

import { ColumnStore, STORE_COLUMNS } from './column_store.js';

/**
 * Stitch labelled ColumnStores into one continuous-timeline store (JS port of
 * python stitch.stitch_stores). Sessions are ordered by first-record start;
 * boundary gaps larger than tolerance are recorded but no synthetic rows are
 * inserted. Provenance maps each stitched record range back to its source.
 *
 * @param {Array<{label:string, store:ColumnStore}>} labelledStores
 * @param {number} gapToleranceSecs
 * @returns {{store:ColumnStore, sources:Array, gaps:Array}}
 */
export function stitchStores(labelledStores, gapToleranceSecs = 2.0) {
  const usable = labelledStores.filter((ls) => ls.store && ls.store.n > 0);
  if (usable.length === 0) {
    return { store: new ColumnStore(0), sources: [], gaps: [] };
  }
  usable.sort((a, b) => a.store.startMs[0] - b.store.startMs[0]);

  const total = usable.reduce((n, ls) => n + ls.store.n, 0);
  const out = new ColumnStore(total);
  const sources = [];
  const gaps = [];
  let pos = 0;
  let prevEndMs = null;
  let prevLabel = null;

  for (const { label, store } of usable) {
    const lo = pos;
    const curStartMs = store.startMs[0];
    if (prevEndMs !== null) {
      const deltaS = (curStartMs - prevEndMs) / 1000;
      if (Math.abs(deltaS) > gapToleranceSecs) {
        gaps.push({
          after_label: prevLabel,
          before_label: label,
          t_gap_start: new Date(prevEndMs).toISOString(),
          t_gap_end: new Date(curStartMs).toISOString(),
          seconds: deltaS,
        });
      }
    }
    for (const name of STORE_COLUMNS) out.cols[name].set(store.cols[name], pos);
    out.startMs.set(store.startMs, pos);
    out.endMs.set(store.endMs, pos);
    pos += store.n;
    const hi = pos;
    sources.push({
      label, lo, hi,
      t_start: new Date(store.startMs[0]).toISOString(),
      t_end: new Date(store.endMs[store.n - 1]).toISOString(),
      records: store.n,
    });
    prevEndMs = store.endMs[store.n - 1];
    prevLabel = label;
  }
  return { store: out, sources, gaps };
}

export class MultiSession {
  constructor() {
    /** @type {Array<{label:string, records:Array, events:Array, snapshots:Array, findings:Array, config:object|null, fileHash:string|null, color:string}>} */
    this.sessions = [];
    this.activeIdx = -1;
    this.compareMode = false;
    this.listeners = new Set();
  }

  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  _emit() { for (const fn of this.listeners) try { fn(this); } catch (_) {} }

  add(session) {
    const colors = ['#cc0000', '#0066cc', '#009933', '#660066', '#cc6600', '#0099aa'];
    const idx = this.sessions.length;
    const labelBase = session.label || (session.config?.asset_name) || `session ${idx + 1}`;
    let label = labelBase;
    let suffix = 2;
    while (this.sessions.some((s) => s.label === label)) {
      label = `${labelBase}-${suffix++}`;
    }
    this.sessions.push({
      ...session,
      label,
      color: colors[idx % colors.length],
    });
    this.activeIdx = this.sessions.length - 1;
    this._emit();
    return this.sessions[this.activeIdx];
  }

  remove(label) {
    const idx = this.sessions.findIndex((s) => s.label === label);
    if (idx < 0) return false;
    this.sessions.splice(idx, 1);
    if (this.activeIdx >= this.sessions.length) {
      this.activeIdx = this.sessions.length - 1;
    }
    if (this.sessions.length < 2) this.compareMode = false;
    this._emit();
    return true;
  }

  rename(oldLabel, newLabel) {
    const s = this.sessions.find((x) => x.label === oldLabel);
    if (!s) return false;
    newLabel = newLabel.trim();
    if (!newLabel || newLabel === oldLabel) return false;
    // Allow rename only if newLabel isn't taken
    if (this.sessions.some((x) => x.label === newLabel)) return false;
    s.label = newLabel;
    this._emit();
    return true;
  }

  setActive(label) {
    const idx = this.sessions.findIndex((s) => s.label === label);
    if (idx < 0) return false;
    this.activeIdx = idx;
    this._emit();
    return true;
  }

  setCompareMode(on) {
    if (this.sessions.length < 2) on = false;
    this.compareMode = !!on;
    this._emit();
  }

  count() { return this.sessions.length; }
  canCompare() { return this.sessions.length >= 2; }

  /**
   * Build a single stitched ColumnStore across all loaded sessions (Feature D).
   * Each session contributes its ColumnStore (or one built from its records).
   * @param {object} spec parsed field_map.json (needed when a session only has records)
   * @param {number} [gapToleranceSecs]
   * @returns {{store: ColumnStore, sources: Array, gaps: Array}}
   */
  buildStitched(spec, gapToleranceSecs = 2.0) {
    const labelled = this.sessions.map((s) => ({
      label: s.label,
      store: s.store || (s.records ? ColumnStore.fromRecords(s.records, spec) : new ColumnStore(0)),
    }));
    return stitchStores(labelled, gapToleranceSecs);
  }
  getActive() { return this.activeIdx < 0 ? null : this.sessions[this.activeIdx]; }
  getAll() { return this.sessions.slice(); }
  clear() {
    this.sessions = [];
    this.activeIdx = -1;
    this.compareMode = false;
    this._emit();
  }
}
