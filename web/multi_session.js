// Multi-session state container. Each loaded file (binary / .fel / .csv)
// becomes one entry. Single-session view uses the "active" entry;
// compare mode iterates the whole list.

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
  getActive() { return this.activeIdx < 0 ? null : this.sessions[this.activeIdx]; }
  getAll() { return this.sessions.slice(); }
  clear() {
    this.sessions = [];
    this.activeIdx = -1;
    this.compareMode = false;
    this._emit();
  }
}
