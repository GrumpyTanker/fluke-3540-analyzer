// Round-2 analysis — JS port of python/src/fluke_3540/analysis.py.
//
// Mirrors the Python algorithms numerically so the web output matches the CLI:
//   - RunningMoments  (Welford running mean/variance)
//   - PercentileSketch (fixed-width streaming histogram)
//   - wholeSessionStats
//   - classifyItic / eventItic (ITIC/CBEMA ride-through)
//   - parsePeriod / bucketKey / bucketLabel / bucketSummaryRow
//   - timeOfDayProfile
//   - correlateMarkers
//
// All functions accept a ColumnStore OR a records array via asColumnSource.

import { asColumnSource } from './column_source.js';

// --- Streaming percentile sketch -------------------------------------------

export class PercentileSketch {
  constructor(lo, hi, nbins = 4000) {
    if (hi <= lo) hi = lo + 1.0;
    this.lo = lo;
    this.hi = hi;
    this.nbins = nbins;
    this.bins = new Int32Array(nbins);
    this.width = (hi - lo) / nbins;
    this.n = 0;
    this.minV = Infinity;
    this.maxV = -Infinity;
  }

  add(v) {
    if (v !== v) return;  // NaN
    this.n += 1;
    if (v < this.minV) this.minV = v;
    if (v > this.maxV) this.maxV = v;
    let idx = Math.floor((v - this.lo) / this.width);
    if (idx < 0) idx = 0;
    else if (idx >= this.nbins) idx = this.nbins - 1;
    this.bins[idx] += 1;
  }

  quantile(q) {
    if (this.n === 0) return NaN;
    const target = q * this.n;
    let cum = 0;
    for (let i = 0; i < this.nbins; i++) {
      cum += this.bins[i];
      if (cum >= target) return this.lo + (i + 0.5) * this.width;
    }
    return this.hi;
  }
}

export class RunningMoments {
  constructor() { this.n = 0; this.mean = 0.0; this.m2 = 0.0; }
  add(v) {
    if (v !== v) return;
    this.n += 1;
    const delta = v - this.mean;
    this.mean += delta / this.n;
    this.m2 += delta * (v - this.mean);
  }
  get variance() { return this.n > 0 ? this.m2 / this.n : 0.0; }
  get stdev() { return Math.sqrt(this.variance); }
}

// Channels reported in whole-session stats, with histogram ranges + units.
// Matches python analysis._STATS_CHANNELS exactly.
export const STATS_CHANNELS = [
  ['V_LN_a_avg_V', 0.0, 400.0, 'V'],
  ['V_LN_b_avg_V', 0.0, 400.0, 'V'],
  ['V_LN_c_avg_V', 0.0, 400.0, 'V'],
  ['I_a_avg_A', 0.0, 1000.0, 'A'],
  ['I_b_avg_A', 0.0, 1000.0, 'A'],
  ['I_c_avg_A', 0.0, 1000.0, 'A'],
  ['freq_avg_Hz', 55.0, 65.0, 'Hz'],
  ['P_total_avg_W', -2000000.0, 2000000.0, 'W'],
  ['S_total_avg_VA', -2000000.0, 2000000.0, 'VA'],
  ['Q_total_avg_VAR', -2000000.0, 2000000.0, 'VAR'],
  ['PF_total_avg', -1.05, 1.05, ''],
];

/**
 * Per-channel streaming statistics + threshold time accounting.
 * @param {import('./column_store.js').ColumnStore|Array} source
 * @param {object} spec
 * @param {{undervoltageV?:number, overcurrentA?:number}} [opts]
 * @returns {object} keyed by channel name + "_thresholds"
 */
export function wholeSessionStats(source, spec, opts = {}) {
  const undervoltageV = opts.undervoltageV ?? 250.0;
  const overcurrentA = opts.overcurrentA ?? 800.0;
  const src = asColumnSource(source, spec);
  const nrec = src.length;
  const out = {};
  const moments = {};
  const sketches = {};
  const cols = {};
  for (const [name, lo, hi] of STATS_CHANNELS) {
    moments[name] = new RunningMoments();
    sketches[name] = new PercentileSketch(lo, hi);
    cols[name] = src.column(name);
  }
  const va = src.column('V_LN_a_avg_V');
  const vb = src.column('V_LN_b_avg_V');
  const vc = src.column('V_LN_c_avg_V');
  const ia = src.column('I_a_avg_A');
  const ib = src.column('I_b_avg_A');
  const ic = src.column('I_c_avg_A');

  let secUnder = 0;
  let secOver = 0;
  for (let i = 0; i < nrec; i++) {
    for (const [name] of STATS_CHANNELS) {
      const v = cols[name][i];
      moments[name].add(v);
      sketches[name].add(v);
    }
    const notOutage = va[i] > 50.0 && vb[i] > 50.0 && vc[i] > 50.0;
    if (notOutage && (va[i] < undervoltageV || vb[i] < undervoltageV || vc[i] < undervoltageV)) {
      secUnder += 1;
    }
    if (ia[i] > overcurrentA || ib[i] > overcurrentA || ic[i] > overcurrentA) {
      secOver += 1;
    }
  }

  for (const [name, , , unit] of STATS_CHANNELS) {
    const m = moments[name];
    const sk = sketches[name];
    if (m.n === 0) continue;
    out[name] = {
      unit,
      count: m.n,
      min: sk.minV,
      p1: sk.quantile(0.01),
      p5: sk.quantile(0.05),
      median: sk.quantile(0.50),
      mean: m.mean,
      p95: sk.quantile(0.95),
      p99: sk.quantile(0.99),
      max: sk.maxV,
      stdev: m.stdev,
    };
  }
  out._thresholds = {
    undervoltage_v: undervoltageV,
    sec_undervoltage: secUnder,
    pct_undervoltage: nrec ? (secUnder / nrec) * 100 : 0.0,
    overcurrent_a: overcurrentA,
    sec_overcurrent: secOver,
    pct_overcurrent: nrec ? (secOver / nrec) * 100 : 0.0,
    total_records: nrec,
  };
  return out;
}

// --- ITIC / CBEMA classification -------------------------------------------

const ITIC_LOWER = [
  [0.001, 0.0],
  [0.003, 0.0],
  [0.020, 70.0],
  [0.500, 70.0],
  [10.0, 80.0],
  [1e9, 90.0],
];
const ITIC_UPPER = [
  [0.001, 500.0],
  [0.0001, 500.0],
  [0.003, 200.0],
  [0.5, 120.0],
  [10.0, 120.0],
  [1e9, 110.0],
];

function interpStep(table, duration) {
  for (const [dmax, val] of table) {
    if (duration <= dmax) return val;
  }
  return table[table.length - 1][1];
}

export function classifyItic(residualPct, durationSecs) {
  if (durationSecs < 0) durationSecs = 0.0;
  const lower = interpStep(ITIC_LOWER, durationSecs);
  const upper = interpStep(ITIC_UPPER, durationSecs);
  if (residualPct > upper) return 'prohibited';
  if (residualPct < lower) return 'no_damage';
  return 'no_interruption';
}

/**
 * ITIC inputs + classification for a dip/outage/swell event.
 * @param {{kind:string, tStartMs:number, tEndMs:number, severity:number}} ev
 * @param {number} nominalLnV
 */
export function eventItic(ev, nominalLnV) {
  const duration = (ev.tEndMs - ev.tStartMs) / 1000;
  let residualPct;
  if (ev.kind === 'dip') residualPct = ev.severity * 100.0;
  else if (ev.kind === 'outage') residualPct = nominalLnV ? (ev.severity / nominalLnV) * 100.0 : 0.0;
  else if (ev.kind === 'swell') residualPct = ev.severity * 100.0;
  else return {};
  return {
    residual_pct: residualPct,
    duration_secs: duration,
    itic_class: classifyItic(residualPct, duration),
  };
}

// --- Time-bucket partitioning (--split-by) ---------------------------------

export function parsePeriod(text) {
  const t = String(text).trim().toLowerCase();
  if (t === 'hour' || t === 'hourly') return { kind: 'hour', seconds: 3600 };
  if (t === 'day' || t === 'daily') return { kind: 'day', seconds: 86400 };
  if (t === 'week' || t === 'weekly') return { kind: 'week', seconds: 7 * 86400 };
  const units = { s: 1, m: 60, h: 3600, d: 86400 };
  const unit = t.slice(-1);
  const num = t.slice(0, -1);
  if (t && units[unit] !== undefined && /^\d+$/.test(num)) {
    const n = parseInt(num, 10);
    if (n <= 0) throw new Error(`--split-by duration must be positive: ${text}`);
    return { kind: 'duration', seconds: n * units[unit] };
  }
  throw new Error(
    `Unrecognized --split-by period ${text}. Use hour|day|week or a duration like 30m, 6h, 2d.`
  );
}

// Time-of-day profile binning by minute-of-day (UTC), mirroring python.
export function parseTodWindow(text) {
  const [a, b] = String(text).split('-');
  const toMin = (s) => {
    const [hh, mm] = String(s).trim().split(':');
    return parseInt(hh, 10) * 60 + (mm ? parseInt(mm, 10) : 0);
  };
  let start = toMin(a);
  let end = toMin(b);
  if (end === 0) end = 1440;
  return [start, end];
}

/**
 * Diurnal avg/min/max envelope per time-of-day bin (UTC clock).
 * @returns {Array<object>} rows with bin/n/n_days/p_avg_kW.../v.../i...
 */
export function timeOfDayProfile(source, spec, opts = {}) {
  const [startMin, endMin] = opts.window ?? [0, 1440];
  const binMinutes = Math.max(1, opts.binMinutes ?? 1);
  const src = asColumnSource(source, spec);
  const nbins = Math.floor((1440 + binMinutes - 1) / binMinutes);
  const p = src.column('P_total_avg_W');
  const va = src.column('V_LN_a_avg_V');
  const ia = src.column('I_a_avg_A');

  const agg = new Array(nbins).fill(null);
  const daysPerBin = Array.from({ length: nbins }, () => new Set());

  for (let i = 0; i < src.length; i++) {
    const d = new Date(src.startMs(i));
    const mod = d.getUTCHours() * 60 + d.getUTCMinutes();
    if (!(startMin <= mod && mod < endMin)) continue;
    let b = Math.floor(mod / binMinutes);
    if (b >= nbins) b = nbins - 1;
    const pv = p[i] / 1000.0;
    if (agg[b] === null) {
      agg[b] = {
        n: 0,
        pSum: 0, pMin: Infinity, pMax: -Infinity,
        vSum: 0, vMin: Infinity, vMax: -Infinity,
        iSum: 0, iMin: Infinity, iMax: -Infinity,
      };
    }
    const a = agg[b];
    a.n += 1;
    a.pSum += pv; a.pMin = Math.min(a.pMin, pv); a.pMax = Math.max(a.pMax, pv);
    a.vSum += va[i]; a.vMin = Math.min(a.vMin, va[i]); a.vMax = Math.max(a.vMax, va[i]);
    a.iSum += ia[i]; a.iMin = Math.min(a.iMin, ia[i]); a.iMax = Math.max(a.iMax, ia[i]);
    // Day key = UTC date string.
    daysPerBin[b].add(d.toISOString().slice(0, 10));
  }

  const rows = [];
  for (let b = 0; b < nbins; b++) {
    const a = agg[b];
    if (a === null || a.n === 0) continue;
    const binStartMin = b * binMinutes;
    const hh = String(Math.floor(binStartMin / 60)).padStart(2, '0');
    const mm = String(binStartMin % 60).padStart(2, '0');
    rows.push({
      bin: `${hh}:${mm}`,
      n: a.n,
      n_days: daysPerBin[b].size,
      p_avg_kW: a.pSum / a.n,
      p_min_kW: a.pMin,
      p_max_kW: a.pMax,
      v_avg_V: a.vSum / a.n,
      v_min_V: a.vMin,
      v_max_V: a.vMax,
      i_avg_A: a.iSum / a.n,
      i_min_A: a.iMin,
      i_max_A: a.iMax,
    });
  }
  return rows;
}

// --- Event markers / correlation -------------------------------------------

/**
 * For each marker, find the nearest detected event + signed offset (s).
 * @param {Array<{timeMs:number, label:string}>} markers
 * @param {Array<{id:number, kind:string, tStartMs:number}>} events
 */
export function correlateMarkers(markers, events) {
  const out = [];
  for (const m of markers) {
    let nearest = null;
    let best = null;
    for (const ev of events) {
      const off = (m.timeMs - ev.tStartMs) / 1000;
      if (best === null || Math.abs(off) < Math.abs(best)) {
        best = off;
        nearest = ev;
      }
    }
    const entry = {
      marker_time: new Date(m.timeMs).toISOString(),
      label: m.label,
      nearest_event: null,
    };
    if (nearest !== null) {
      entry.nearest_event = {
        id: nearest.id,
        kind: nearest.kind,
        t_start: new Date(nearest.tStartMs).toISOString(),
        offset_secs: best,
      };
    }
    out.push(entry);
  }
  return out;
}
