// Columnar session store — JS port of python/src/fluke_3540/store.py.
//
// The legacy web path materialised every record as a {index, startMs, endMs,
// floats:Float32Array(180)} object. For a week-long capture (~590 K records)
// that is ~1.6 GB of heap. This ColumnStore keeps only the ~24 analysis
// channels the event / snapshot / insight / stats engines actually read, each
// as a packed Float32Array, plus a Float64Array of start/end millisecond
// timestamps. That is ~55 MB for a full week instead of >1 GB, and the typed
// arrays are Transferable so the worker hands them back with zero copy.
//
// STORE_COLUMNS mirrors python store.STORE_COLUMNS exactly so the two analysis
// paths read identical channels.

export const STORE_COLUMNS = Object.freeze([
  // Per-phase L-N voltage min/max/avg
  'V_LN_a_min_V', 'V_LN_b_min_V', 'V_LN_c_min_V',
  'V_LN_a_max_V', 'V_LN_b_max_V', 'V_LN_c_max_V',
  'V_LN_a_avg_V', 'V_LN_b_avg_V', 'V_LN_c_avg_V',
  // Per-phase current max + avg
  'I_a_max_A', 'I_b_max_A', 'I_c_max_A',
  'I_a_avg_A', 'I_b_avg_A', 'I_c_avg_A',
  // Line frequency
  'freq_avg_Hz',
  // Power / apparent / reactive / power-factor totals
  'P_total_avg_W', 'S_total_avg_VA', 'Q_total_avg_VAR', 'PF_total_avg',
  // Per-row energy (per-bucket kWh roll-ups)
  'Wh_total',
  // THD per phase (IEEE 519) — V and I, avg only
  'V_THD_pct_a_avg', 'V_THD_pct_b_avg', 'V_THD_pct_c_avg',
  'I_THD_pct_a_avg', 'I_THD_pct_b_avg', 'I_THD_pct_c_avg',
]);

/**
 * Resolve STORE_COLUMNS to spec float indices, once.
 * @param {object} spec parsed field_map.json
 * @returns {Map<string, number>} name -> spec float index
 */
export function resolveStoreIndices(spec) {
  const nameToIdx = new Map(spec.fields.map((f) => [f.name, f.index]));
  const out = new Map();
  for (const name of STORE_COLUMNS) {
    const idx = nameToIdx.get(name);
    if (idx === undefined) {
      throw new Error(`Store column ${name} missing from spec/field_map.json`);
    }
    out.set(name, idx);
  }
  return out;
}

export class ColumnStore {
  /**
   * @param {number} n record count (used to pre-size typed arrays)
   */
  constructor(n = 0) {
    this.n = n;
    this.cols = {};
    for (const name of STORE_COLUMNS) this.cols[name] = new Float32Array(n);
    this.startMs = new Float64Array(n);
    this.endMs = new Float64Array(n);
  }

  /** Packed Float32Array column for `name` (no copy). */
  col(name) {
    const c = this.cols[name];
    if (c === undefined) {
      throw new Error(
        `Column ${name} is not retained in the ColumnStore. ` +
        `Retained columns: ${STORE_COLUMNS.join(', ')}`
      );
    }
    return c;
  }

  start(i) { return this.startMs[i]; }
  end(i) { return this.endMs[i]; }

  get firstStartMs() { return this.n ? this.startMs[0] : null; }
  get lastEndMs() { return this.n ? this.endMs[this.n - 1] : null; }

  /**
   * Lazily yield a lightweight record view ({index, startMs, endMs, floats})
   * for each record. The `floats` proxy only carries the retained columns at
   * their spec index; reads of non-retained indices return 0. Used to feed the
   * existing record-array consumers (events.js, snapshots.js, insights.js)
   * without holding 590 K full record objects.
   *
   * @param {object} spec
   * @returns {Array} array of record-shaped views
   */
  toRecords(spec) {
    const idxByName = resolveStoreIndices(spec);
    const dataFloats = spec.data_floats;
    const colArrays = STORE_COLUMNS.map((name) => [idxByName.get(name), this.cols[name]]);
    const out = new Array(this.n);
    for (let i = 0; i < this.n; i++) {
      const floats = new Float32Array(dataFloats);
      for (const [idx, arr] of colArrays) floats[idx] = arr[i];
      out[i] = { index: i, startMs: this.startMs[i], endMs: this.endMs[i], floats };
    }
    return out;
  }

  /**
   * Reconstruct a ColumnStore from a worker "done-columnar" payload:
   * { recordCount, columns: {name: Float32Array}, startMs, endMs }.
   */
  static fromTransfer(payload) {
    const store = Object.create(ColumnStore.prototype);
    store.n = payload.recordCount;
    store.cols = payload.columns;
    store.startMs = payload.startMs;
    store.endMs = payload.endMs;
    return store;
  }

  /**
   * Build a ColumnStore from an array of record objects (legacy small-file /
   * test path). Mirrors python ColumnStore.from_records.
   * @param {Array<{startMs:number, endMs:number, floats:ArrayLike<number>}>} records
   * @param {object} spec
   */
  static fromRecords(records, spec) {
    const idxByName = resolveStoreIndices(spec);
    const store = new ColumnStore(records.length);
    const colArrays = STORE_COLUMNS.map((name) => [idxByName.get(name), store.cols[name]]);
    for (let i = 0; i < records.length; i++) {
      const r = records[i];
      for (const [idx, arr] of colArrays) arr[i] = r.floats[idx];
      store.startMs[i] = r.startMs;
      store.endMs[i] = r.endMs;
    }
    return store;
  }
}
