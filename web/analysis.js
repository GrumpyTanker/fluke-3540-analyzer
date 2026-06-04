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

// --- CT-reversal auto-detection (Feature C) --------------------------------
//
// Mirrors python analysis.detect_ct_reversal: a load wired with backwards iFlex
// CTs reads as a persistent generator (P_total < 0). Flag when real power is
// negative for a high fraction of NON-OUTAGE time.

export function detectCtReversal(source, spec, opts = {}) {
  const negFractionThreshold = opts.negFractionThreshold ?? 0.50;
  const outageVThreshold = opts.outageVThreshold ?? 50.0;
  const src = asColumnSource(source, spec);
  const p = src.column('P_total_avg_W');
  const va = src.column('V_LN_a_avg_V');
  const vb = src.column('V_LN_b_avg_V');
  const vc = src.column('V_LN_c_avg_V');
  let nonOutage = 0;
  let negative = 0;
  let pSum = 0.0;
  let pCount = 0;
  for (let i = 0; i < src.length; i++) {
    if (va[i] > outageVThreshold && vb[i] > outageVThreshold && vc[i] > outageVThreshold) {
      nonOutage += 1;
      const pv = p[i];
      if (Number.isFinite(pv)) { pSum += pv; pCount += 1; }
      if (pv < 0) negative += 1;  // NaN < 0 is false, so non-finite never counts
    }
  }
  const frac = nonOutage ? negative / nonOutage : 0.0;
  const meanP = pCount ? pSum / pCount : 0.0;
  return {
    reversed: frac >= negFractionThreshold,
    frac_negative: frac,
    non_outage_records: nonOutage,
    negative_records: negative,
    mean_p_w: meanP,
    threshold: negFractionThreshold,
  };
}

export function ctReversalNotice(result) {
  const pct = result.frac_negative * 100.0;
  return (
    'CT REVERSAL DETECTED — ' +
    `real power (P_total) is negative for ${pct.toFixed(1)}% of non-outage time ` +
    `(mean P = ${(result.mean_p_w / 1000).toFixed(1)} kW). A load should draw ` +
    'positive real power: one or more iFlex CT probes are likely clipped on ' +
    'backwards. Toggle "Reverse CTs" (all phases) to correct P/Q/PF/energy.'
  );
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

// --- IEEE 519 THD + IEEE 1159 / SARFI (Feature F) --------------------------

export const IEEE519_V_THD_LIMIT_PCT = 8.0;
export const IEEE519_V_THD_PLANNING_PCT = 5.0;

export function ieee519Compliance(source, spec) {
  const src = asColumnSource(source, spec);
  const out = {
    limit_v_thd_pct: IEEE519_V_THD_LIMIT_PCT,
    planning_v_thd_pct: IEEE519_V_THD_PLANNING_PCT,
    voltage: {},
    current: {},
  };
  let allCompliant = true;
  for (const ph of ['a', 'b', 'c']) {
    const vcol = src.column(`V_THD_pct_${ph}_avg`);
    const sk = new PercentileSketch(0.0, 100.0, 2000);
    for (let i = 0; i < vcol.length; i++) sk.add(vcol[i]);
    const p95 = sk.n ? sk.quantile(0.95) : 0.0;
    const compliant = p95 <= IEEE519_V_THD_LIMIT_PCT;
    allCompliant = allCompliant && compliant;
    out.voltage[ph] = {
      p95,
      limit: IEEE519_V_THD_LIMIT_PCT,
      planning: IEEE519_V_THD_PLANNING_PCT,
      compliant,
      exceeds_planning: p95 > IEEE519_V_THD_PLANNING_PCT,
    };
    const icol = src.column(`I_THD_pct_${ph}_avg`);
    const ski = new PercentileSketch(0.0, 200.0, 2000);
    for (let i = 0; i < icol.length; i++) ski.add(icol[i]);
    out.current[ph] = { p95: ski.n ? ski.quantile(0.95) : 0.0 };
  }
  out.all_voltage_compliant = allCompliant;
  return out;
}

export const SARFI_THRESHOLDS = [90, 80, 70, 50, 10];

export function sarfiIndices(events, nominalLnV) {
  const counts = {};
  for (const x of SARFI_THRESHOLDS) counts[`SARFI-${x}`] = 0;
  let considered = 0;
  for (const ev of events) {
    let residualPct;
    if (ev.kind === 'dip') residualPct = ev.severity * 100.0;
    else if (ev.kind === 'outage') residualPct = nominalLnV ? (ev.severity / nominalLnV) * 100.0 : 0.0;
    else continue;
    considered += 1;
    for (const x of SARFI_THRESHOLDS) {
      if (residualPct < x) counts[`SARFI-${x}`] += 1;
    }
  }
  counts.events_considered = considered;
  counts.nominal_ln_v = nominalLnV;
  return counts;
}

// --- Demand analysis (Feature G) -------------------------------------------

export function demandAnalysis(source, spec, opts = {}) {
  const windowSecs = Math.max(1, Math.floor(opts.windowSecs ?? 900));
  const seriesStepSecs = Math.max(0, Math.floor(opts.seriesStepSecs ?? 0));
  const src = asColumnSource(source, spec);
  const p = src.column('P_total_avg_W');
  const n = src.length;
  const out = {
    window_secs: windowSecs,
    peak_demand_w: 0.0,
    peak_demand_kw: 0.0,
    peak_window_end: null,
    peak_window_start: null,
    mean_demand_w: 0.0,
    n_windows: 0,
    series: [],
  };
  if (n === 0) return out;
  const finite = (v) => (Number.isFinite(v) ? v : 0.0);
  let running = 0.0;
  let peak = -Infinity;
  let peakI = -1;
  let demandSum = 0.0;
  let demandCount = 0;
  const series = [];
  const w = windowSecs;
  for (let i = 0; i < n; i++) {
    running += finite(p[i]);
    if (i >= w) running -= finite(p[i - w]);
    if (i >= w - 1) {
      const demand = running / w;
      demandSum += demand;
      demandCount += 1;
      if (demand > peak) { peak = demand; peakI = i; }
      if (seriesStepSecs && ((i - (w - 1)) % seriesStepSecs === 0)) {
        series.push({ t: new Date(src.endMs(i)).toISOString(), demand_w: demand });
      }
    }
  }
  if (peakI >= 0) {
    out.peak_demand_w = peak;
    out.peak_demand_kw = peak / 1000.0;
    out.peak_window_end = new Date(src.endMs(peakI)).toISOString();
    out.peak_window_start = new Date(src.startMs(peakI - w + 1)).toISOString();
    out.mean_demand_w = demandCount ? demandSum / demandCount : 0.0;
    out.n_windows = demandCount;
  }
  out.series = series;
  return out;
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

// --- Generalized named shift/period splitting (--split-by shifts) ----------
// JS port of python/.../analysis.py Shift / ShiftSet + aggregate / occurrences
// / comparison. The store holds UTC epoch ms; shift windows are evaluated in
// the REPORT timezone (tzName, IANA) — localize minute-of-day BEFORE applying
// the HH:MM rule, exactly like the Python side. tzName null/'UTC' = UTC.

export const UNASSIGNED_SHIFT = 'unassigned';

function parseHhmm(text) {
  const s = String(text).trim();
  if (!s.includes(':')) throw new Error(`shift time must be HH:MM (with a colon): ${text}`);
  const [hh, mm] = s.split(':');
  if (!/^\d+$/.test(hh) || !/^\d+$/.test(mm)) {
    throw new Error(`shift time must be numeric HH:MM: ${text}`);
  }
  const h = parseInt(hh, 10);
  const m = parseInt(mm, 10);
  if (m >= 60) throw new Error(`shift minutes out of range in ${text}`);
  const total = h * 60 + m;
  if (total < 0 || total > 1440) throw new Error(`shift time out of range (00:00..24:00): ${text}`);
  return total;
}

export class Shift {
  constructor(name, startMin, endMin) {
    this.name = name;
    this.startMin = startMin;
    this.endMin = endMin;
  }

  get wraps() { return this.endMin <= this.startMin; }

  containsMinute(mod) {
    if (!this.wraps) return this.startMin <= mod && mod < this.endMin;
    return mod >= this.startMin || mod < this.endMin;
  }

  lengthMinutes() {
    return this.wraps ? (1440 - this.startMin) + this.endMin : this.endMin - this.startMin;
  }

  get windowStr() {
    const fmt = (m) => (m === 1440
      ? '24:00'
      : `${String(Math.floor(m / 60) % 24).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`);
    return `${fmt(this.startMin)}-${fmt(this.endMin)}`;
  }
}

export class ShiftSet {
  constructor(shifts) { this.shifts = shifts; }

  static parse(text) {
    const out = [];
    const seen = new Set();
    const parts = String(text).split(',').map((p) => p.trim()).filter(Boolean);
    if (!parts.length) throw new Error(`no shifts parsed from ${text}`);
    for (const part of parts) {
      const eq = part.indexOf('=');
      if (eq <= 0) throw new Error(`shift must be name=HH:MM-HH:MM: ${part}`);
      const name = part.slice(0, eq).trim();
      const window = part.slice(eq + 1);
      const dash = window.indexOf('-');
      if (dash < 0) throw new Error(`shift window must be HH:MM-HH:MM: ${window}`);
      const start = parseHhmm(window.slice(0, dash));
      const end = parseHhmm(window.slice(dash + 1));
      if (start === end) throw new Error(`shift ${name} has a zero-length window ${window}`);
      if (seen.has(name)) throw new Error(`duplicate shift name ${name}`);
      seen.add(name);
      out.push(new Shift(name, start, end));
    }
    return new ShiftSet(out);
  }

  static fromSpec(spec) {
    return ShiftSet.parse(spec.map((s) => `${s.name}=${s.start}-${s.end}`).join(','));
  }

  static default() {
    return ShiftSet.parse('day=06:00-18:00,night=18:00-06:00');
  }

  coverageIssues() {
    const cover = new Array(1440).fill(0);
    for (const sh of this.shifts) {
      for (let m = 0; m < 1440; m++) if (sh.containsMinute(m)) cover[m] += 1;
    }
    const gap = cover.filter((c) => c === 0).length;
    const overlap = cover.filter((c) => c > 1).length;
    const issues = [];
    if (gap) {
      issues.push(`${gap} minute(s)/day fall in NO shift window (gap); those `
        + `records go to '${UNASSIGNED_SHIFT}'.`);
    }
    if (overlap) {
      issues.push(`${overlap} minute(s)/day are covered by MORE THAN ONE shift `
        + '(overlap); the first matching window wins.');
    }
    return issues;
  }
}

/**
 * Minute-of-day for epoch `ms` in the report timezone (UTC if tzName falsy).
 * Uses Intl (same approach as tzutil.isoInZone) so it matches Python's
 * datetime.astimezone(ZoneInfo(tz)) wall clock.
 */
export function localMinuteOfDay(ms, tzName) {
  if (!tzName || String(tzName).toUpperCase() === 'UTC') {
    const d = new Date(ms);
    return d.getUTCHours() * 60 + d.getUTCMinutes();
  }
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: tzName, hour: '2-digit', minute: '2-digit', hour12: false,
  });
  const parts = {};
  for (const p of fmt.formatToParts(new Date(ms))) parts[p.type] = p.value;
  const hour = parts.hour === '24' ? 0 : parseInt(parts.hour, 10);
  return hour * 60 + parseInt(parts.minute, 10);
}

function localDateStr(ms, tzName) {
  if (!tzName || String(tzName).toUpperCase() === 'UTC') {
    return new Date(ms).toISOString().slice(0, 10);
  }
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: tzName, year: 'numeric', month: '2-digit', day: '2-digit',
  });
  const parts = {};
  for (const p of fmt.formatToParts(new Date(ms))) parts[p.type] = p.value;
  return `${parts.year}-${parts.month}-${parts.day}`;
}

/**
 * Group record indices by shift NAME, evaluated in tzName. Returns a plain
 * object {name: [indices]}; every named shift key is present (possibly empty);
 * unmatched records collect under UNASSIGNED_SHIFT (only when non-empty).
 */
export function aggregateShifts(source, spec, ss, opts = {}) {
  const tzName = opts.tz ?? null;
  const src = asColumnSource(source, spec);
  const out = {};
  for (const sh of ss.shifts) out[sh.name] = [];
  for (let i = 0; i < src.length; i++) {
    const mod = localMinuteOfDay(src.startMs(i), tzName);
    let placed = false;
    for (const sh of ss.shifts) {
      if (sh.containsMinute(mod)) { out[sh.name].push(i); placed = true; break; }
    }
    if (!placed) {
      (out[UNASSIGNED_SHIFT] ??= []).push(i);
    }
  }
  return out;
}

/**
 * Contiguous per-occurrence buckets: [{label, name, lo, hi}], time-ordered. A
 * wrap-past-midnight occurrence stays one bucket, labeled by its start date.
 */
export function shiftOccurrences(source, spec, ss, opts = {}) {
  const tzName = opts.tz ?? null;
  const src = asColumnSource(source, spec);
  const n = src.length;
  if (n === 0) return [];
  const nameAt = (i) => {
    const mod = localMinuteOfDay(src.startMs(i), tzName);
    for (const sh of ss.shifts) if (sh.containsMinute(mod)) return sh.name;
    return UNASSIGNED_SHIFT;
  };
  const out = [];
  let curName = nameAt(0);
  let lo = 0;
  for (let i = 1; i < n; i++) {
    const nm = nameAt(i);
    if (nm !== curName) {
      out.push({ label: `${curName} ${localDateStr(src.startMs(lo), tzName)}`, name: curName, lo, hi: i });
      curName = nm; lo = i;
    }
  }
  out.push({ label: `${curName} ${localDateStr(src.startMs(lo), tzName)}`, name: curName, lo, hi: n });
  return out;
}

/**
 * The headline per-shift-name aggregate comparison. One row per named shift
 * (plus unassigned if any). `events` are filed by the shift their tStartMs
 * falls in. demandWindow is in MINUTES.
 * @returns {Array<object>} rows mirroring python shift_comparison_rows
 */
export function shiftComparisonRows(source, spec, ss, opts = {}) {
  const tzName = opts.tz ?? null;
  const events = opts.events ?? [];
  const demandMin = Math.max(1, Math.floor(opts.demandWindow ?? 15));
  const src = asColumnSource(source, spec);
  const byName = aggregateShifts(source, spec, ss, { tz: tzName });
  const winByName = new Map(ss.shifts.map((sh) => [sh.name, sh]));

  const evByName = {};
  for (const nm of Object.keys(byName)) evByName[nm] = [];
  for (const e of events) {
    const mod = localMinuteOfDay(e.tStartMs, tzName);
    let placed = false;
    for (const sh of ss.shifts) {
      if (sh.containsMinute(mod)) { (evByName[sh.name] ??= []).push(e); placed = true; break; }
    }
    if (!placed) (evByName[UNASSIGNED_SHIFT] ??= []).push(e);
  }

  const p = src.column('P_total_avg_W');
  const pf = src.column('PF_total_avg');
  const va = src.column('V_LN_a_avg_V');
  const vb = src.column('V_LN_b_avg_V');
  const vc = src.column('V_LN_c_avg_V');
  const vthA = src.column('V_THD_pct_a_avg');
  const vthB = src.column('V_THD_pct_b_avg');
  const vthC = src.column('V_THD_pct_c_avg');

  const rows = [];
  for (const [name, idxs] of Object.entries(byName)) {
    const sh = winByName.get(name);
    const window = sh ? sh.windowStr : '—';
    const evs = evByName[name] ?? [];

    const pMom = new RunningMoments();
    let pMin = Infinity; let pMax = -Infinity;
    const pfMom = new RunningMoments();
    const vMom = new RunningMoments();
    const vSketch = new PercentileSketch(0.0, 400.0);
    const vthdSketch = new PercentileSketch(0.0, 50.0);
    const gatheredP = new Float64Array(idxs.length);
    for (let k = 0; k < idxs.length; k++) {
      const i = idxs[k];
      const pv = p[i];
      gatheredP[k] = Number.isFinite(pv) ? pv : 0.0;
      if (Number.isFinite(pv)) { pMom.add(pv); pMin = Math.min(pMin, pv); pMax = Math.max(pMax, pv); }
      const pfi = pf[i];
      if (Number.isFinite(pfi)) pfMom.add(pfi);
      for (const vv of [va[i], vb[i], vc[i]]) {
        if (Number.isFinite(vv) && vv > 50.0) { vMom.add(vv); vSketch.add(vv); }
      }
      for (const tv of [vthA[i], vthB[i], vthC[i]]) {
        if (Number.isFinite(tv)) vthdSketch.add(tv);
      }
    }

    const pMean = pMom.n ? pMom.mean : 0.0;
    const hours = pMom.n / 3600.0;
    const kwh = (pMean / 1000.0) * hours;
    const peakKw = peakRollingDemandKw(gatheredP, demandMin * 60);

    let nOut = 0; let nDip = 0; let nSwell = 0; let outageSecs = 0.0;
    for (const e of evs) {
      if (e.kind === 'outage') { nOut += 1; outageSecs += (e.tEndMs - e.tStartMs) / 1000; }
      else if (e.kind === 'dip') nDip += 1;
      else if (e.kind === 'swell') nSwell += 1;
    }
    const q = (sk, qq) => { const v = sk.quantile(qq); return Number.isNaN(v) ? 0.0 : v; };

    rows.push({
      shift: name,
      window,
      records: idxs.length,
      hours,
      kWh: kwh,
      P_total_avg_W: pMean,
      P_total_min_W: pMin === Infinity ? 0.0 : pMin,
      P_total_max_W: pMax === -Infinity ? 0.0 : pMax,
      peak_demand_kW: peakKw,
      peak_demand_window_secs: demandMin * 60,
      PF_avg: pfMom.n ? pfMom.mean : 0.0,
      V_LN_avg_V: vMom.n ? vMom.mean : 0.0,
      V_LN_p5_V: q(vSketch, 0.05),
      V_LN_p95_V: q(vSketch, 0.95),
      V_THD_p95_pct: q(vthdSketch, 0.95),
      n_outages: nOut,
      n_dips: nDip,
      n_swells: nSwell,
      outage_minutes: outageSecs / 60.0,
    });
  }
  return rows;
}

// Trailing rolling-mean peak over a 1-Hz power array, mirroring
// demand_analysis(window_secs).peak_demand_kw on a gathered sub-store.
function peakRollingDemandKw(pArr, windowSecs) {
  const n = pArr.length;
  const w = Math.max(1, Math.floor(windowSecs));
  if (n === 0) return 0.0;
  let running = 0.0;
  let peak = -Infinity;
  let any = false;
  for (let i = 0; i < n; i++) {
    running += pArr[i];
    if (i >= w) running -= pArr[i - w];
    if (i >= w - 1) {
      const demand = running / w;
      if (demand > peak) peak = demand;
      any = true;
    }
  }
  return any ? peak / 1000.0 : 0.0;
}
