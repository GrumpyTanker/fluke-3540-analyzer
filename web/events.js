// Event detection — JS port of python/src/fluke_3540/events.py.
// Mirrors the same thresholds, mask logic, and Event shape.

import { asColumnSource } from './column_source.js';

export const DEFAULT_RULES = Object.freeze({
  outage_v_threshold: 50.0,
  dip_pct_of_nominal: 0.90,
  swell_pct_of_nominal: 1.10,
  high_current_sigma: 2.0,
  freq_excursion_hz: 0.5,
  imbalance_pct_threshold: 2.5,
  power_step_pct_of_mean: 0.50,
  min_duration_secs: 1,
  gap_tolerance_secs: 1,
  nominal_freq_hz: 60.0,
});

const PHASES = ['a', 'b', 'c'];

// Valid EventRules keys for --rules-file overrides (Feature I). Mirrors the
// Python EventRules dataclass fields.
const RULE_KEYS = new Set(Object.keys(DEFAULT_RULES));

/**
 * Resolve per-asset EventRules from a parsed rules-file object (JSON form),
 * mirroring python rules_file.load_rules. Precedence: DEFAULT_RULES ->
 * file.defaults -> file.assets[assetName] (or file.assets.default). A flat
 * object with only rule keys is treated as defaults.
 *
 * @param {object} raw parsed rules object
 * @param {string|null} assetName
 * @returns {object} a rules object suitable for detectEvents({rules})
 */
export function rulesFromObject(raw, assetName = null) {
  if (!raw || typeof raw !== 'object') return { ...DEFAULT_RULES };
  let defaults;
  let assets;
  if ('defaults' in raw || 'assets' in raw) {
    defaults = raw.defaults || {};
    assets = raw.assets || {};
  } else {
    defaults = raw;
    assets = {};
  }
  const coerce = (over, where) => {
    const out = {};
    for (const [k, v] of Object.entries(over)) {
      if (!RULE_KEYS.has(k)) {
        throw new Error(`${where}: unknown EventRules key ${k}`);
      }
      out[k] = (k === 'min_duration_secs' || k === 'gap_tolerance_secs')
        ? Math.trunc(Number(v)) : Number(v);
    }
    return out;
  };
  const merged = { ...DEFAULT_RULES, ...coerce(defaults, 'defaults') };
  let assetOver = {};
  if (assetName && assets[assetName]) assetOver = assets[assetName];
  else if (assets.default) assetOver = assets.default;
  return { ...merged, ...coerce(assetOver, 'assets') };
}

function fieldIndex(spec, name) {
  const f = spec.fields.find((f) => f.name === name);
  if (!f) throw new Error(`spec is missing field ${name}`);
  return f.index;
}

function median(values) {
  if (values.length === 0) return NaN;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0
    ? (sorted[mid - 1] + sorted[mid]) / 2
    : sorted[mid];
}

function mean(values) {
  if (values.length === 0) return NaN;
  let s = 0;
  for (const v of values) s += v;
  return s / values.length;
}

function pstdev(values) {
  if (values.length < 2) return 0;
  const m = mean(values);
  let s = 0;
  for (const v of values) s += (v - m) * (v - m);
  return Math.sqrt(s / values.length);
}

function groupRuns(mask, gapTolerance) {
  const runs = [];
  let inRun = false;
  let start = 0;
  let gap = 0;
  for (let i = 0; i < mask.length; i++) {
    if (mask[i]) {
      if (!inRun) { inRun = true; start = i; }
      gap = 0;
    } else if (inRun) {
      gap++;
      if (gap > gapTolerance) {
        runs.push([start, i - gap]);
        inRun = false;
        gap = 0;
      }
    }
  }
  if (inRun) {
    runs.push([start, mask.length - 1 - Math.max(0, gap)]);
  }
  return runs;
}

function inferNominalLnV(vAvgByPhase, outageThreshold) {
  const pooled = [];
  for (const phaseVals of vAvgByPhase) {
    for (const v of phaseVals) {
      if (v > outageThreshold && Number.isFinite(v)) pooled.push(v);
    }
  }
  if (pooled.length === 0) {
    throw new Error('No non-outage voltage samples — cannot infer nominal V_LN');
  }
  return median(pooled);
}

/**
 * Detect events on an array of parsed Records OR a ColumnStore.
 * @param {Array<{index:number, startMs:number, endMs:number, floats:Float32Array}>|import('./column_store.js').ColumnStore} source
 * @param {object} spec - parsed field_map.json
 * @param {{nominalLnV?: number, rules?: object}} [opts]
 * @returns {Array<Event>}
 */
export function detectEvents(source, spec, opts = {}) {
  const rules = { ...DEFAULT_RULES, ...(opts.rules ?? {}) };
  let { nominalLnV = null } = opts;
  const src = asColumnSource(source, spec);
  if (src.length === 0) return [];

  const N = src.length;
  // Extract columns once into typed arrays for speed (no-copy for a store).
  const vMin = PHASES.map((ph) => src.column(`V_LN_${ph}_min_V`));
  const vMax = PHASES.map((ph) => src.column(`V_LN_${ph}_max_V`));
  const vAvg = PHASES.map((ph) => src.column(`V_LN_${ph}_avg_V`));
  const iMaxArr = PHASES.map((ph) => src.column(`I_${ph}_max_A`));
  const freqArr = src.column('freq_avg_Hz');
  const pTotal = src.column('P_total_avg_W');

  if (nominalLnV === null) {
    nominalLnV = inferNominalLnV(vAvg, rules.outage_v_threshold);
  }
  const dipThreshold = nominalLnV * rules.dip_pct_of_nominal;
  const swellThreshold = nominalLnV * rules.swell_pct_of_nominal;

  const notOutage = new Array(N);
  for (let i = 0; i < N; i++) {
    notOutage[i] =
      vAvg[0][i] > rules.outage_v_threshold &&
      vAvg[1][i] > rules.outage_v_threshold &&
      vAvg[2][i] > rules.outage_v_threshold;
  }
  const outageMask = notOutage.map((b) => !b);

  const events = [];
  const startMs = (i) => src.startMs(i);
  const endMs = (i) => src.endMs(i);

  const phaseChars = (phaseList) => phaseList.slice();

  function rangeMin(arr, s, e) {
    let m = arr[s];
    for (let i = s + 1; i <= e; i++) if (arr[i] < m) m = arr[i];
    return m;
  }
  function rangeMax(arr, s, e) {
    let m = arr[s];
    for (let i = s + 1; i <= e; i++) if (arr[i] > m) m = arr[i];
    return m;
  }

  // outage
  for (const [s, e] of groupRuns(outageMask, rules.gap_tolerance_secs)) {
    if (e - s + 1 < rules.min_duration_secs) continue;
    const lows = vMin.map((arr) => rangeMin(arr, s, e));
    events.push({
      kind: 'outage', tStartMs: startMs(s), tEndMs: endMs(e),
      severity: Math.min(...lows), affectedPhases: phaseChars(PHASES),
    });
  }

  // dip
  const dipMask = new Array(N);
  for (let i = 0; i < N; i++) {
    dipMask[i] = notOutage[i] && (vMin[0][i] < dipThreshold ||
      vMin[1][i] < dipThreshold || vMin[2][i] < dipThreshold);
  }
  for (const [s, e] of groupRuns(dipMask, rules.gap_tolerance_secs)) {
    if (e - s + 1 < rules.min_duration_secs) continue;
    let deepest = Infinity;
    const dipped = [];
    for (let p = 0; p < 3; p++) {
      const phaseMin = rangeMin(vMin[p], s, e);
      if (phaseMin < dipThreshold) {
        dipped.push(PHASES[p]);
        deepest = Math.min(deepest, phaseMin);
      }
    }
    events.push({
      kind: 'dip', tStartMs: startMs(s), tEndMs: endMs(e),
      severity: deepest / nominalLnV, affectedPhases: dipped,
    });
  }

  // swell
  const swellMask = new Array(N);
  for (let i = 0; i < N; i++) {
    swellMask[i] = notOutage[i] && (vMax[0][i] > swellThreshold ||
      vMax[1][i] > swellThreshold || vMax[2][i] > swellThreshold);
  }
  for (const [s, e] of groupRuns(swellMask, rules.gap_tolerance_secs)) {
    if (e - s + 1 < rules.min_duration_secs) continue;
    let highest = -Infinity;
    const swelled = [];
    for (let p = 0; p < 3; p++) {
      const phaseMax = rangeMax(vMax[p], s, e);
      if (phaseMax > swellThreshold) {
        swelled.push(PHASES[p]);
        highest = Math.max(highest, phaseMax);
      }
    }
    events.push({
      kind: 'swell', tStartMs: startMs(s), tEndMs: endMs(e),
      severity: highest / nominalLnV, affectedPhases: swelled,
    });
  }

  // high_current
  for (let p = 0; p < 3; p++) {
    const valid = [];
    for (let i = 0; i < N; i++) if (notOutage[i]) valid.push(iMaxArr[p][i]);
    if (valid.length < 2) continue;
    const mu = mean(valid);
    const sigma = pstdev(valid);
    if (sigma === 0) continue;
    const threshold = mu + rules.high_current_sigma * sigma;
    const mask = new Array(N);
    for (let i = 0; i < N; i++) {
      mask[i] = notOutage[i] && iMaxArr[p][i] > threshold;
    }
    for (const [s, e] of groupRuns(mask, rules.gap_tolerance_secs)) {
      if (e - s + 1 < rules.min_duration_secs) continue;
      const peak = rangeMax(iMaxArr[p], s, e);
      events.push({
        kind: 'high_current', tStartMs: startMs(s), tEndMs: endMs(e),
        severity: peak, affectedPhases: [PHASES[p]],
      });
    }
  }

  // freq_excursion
  const freqMask = new Array(N);
  for (let i = 0; i < N; i++) {
    freqMask[i] = notOutage[i] && Math.abs(freqArr[i] - rules.nominal_freq_hz) > rules.freq_excursion_hz;
  }
  for (const [s, e] of groupRuns(freqMask, rules.gap_tolerance_secs)) {
    if (e - s + 1 < rules.min_duration_secs) continue;
    let worst = freqArr[s];
    for (let i = s + 1; i <= e; i++) {
      if (Math.abs(freqArr[i] - rules.nominal_freq_hz) > Math.abs(worst - rules.nominal_freq_hz)) {
        worst = freqArr[i];
      }
    }
    events.push({
      kind: 'freq_excursion', tStartMs: startMs(s), tEndMs: endMs(e),
      severity: worst - rules.nominal_freq_hz, affectedPhases: [],
    });
  }

  // imbalance_spike
  const imbalMask = new Array(N);
  const imbalPct = new Float64Array(N);
  for (let i = 0; i < N; i++) {
    if (!notOutage[i]) { imbalMask[i] = false; continue; }
    const v0 = vAvg[0][i], v1 = vAvg[1][i], v2 = vAvg[2][i];
    const m = (v0 + v1 + v2) / 3.0;
    if (m <= rules.outage_v_threshold) { imbalMask[i] = false; continue; }
    const pct = (Math.max(v0, v1, v2) - Math.min(v0, v1, v2)) / m * 100.0;
    imbalPct[i] = pct;
    imbalMask[i] = pct > rules.imbalance_pct_threshold;
  }
  for (const [s, e] of groupRuns(imbalMask, rules.gap_tolerance_secs)) {
    if (e - s + 1 < rules.min_duration_secs) continue;
    let peakPct = imbalPct[s];
    for (let i = s + 1; i <= e; i++) if (imbalPct[i] > peakPct) peakPct = imbalPct[i];
    events.push({
      kind: 'imbalance_spike', tStartMs: startMs(s), tEndMs: endMs(e),
      severity: peakPct, affectedPhases: PHASES.slice(),
    });
  }

  // power_step
  const pValid = [];
  for (let i = 0; i < N; i++) if (notOutage[i]) pValid.push(Math.abs(pTotal[i]));
  if (pValid.length >= 2) {
    const baseline = mean(pValid);
    const stepThreshold = baseline * rules.power_step_pct_of_mean;
    if (stepThreshold > 0) {
      for (let i = 1; i < N; i++) {
        if (!(notOutage[i] && notOutage[i - 1])) continue;
        const delta = pTotal[i] - pTotal[i - 1];
        if (Math.abs(delta) > stepThreshold) {
          events.push({
            kind: 'power_step', tStartMs: startMs(i), tEndMs: endMs(i),
            severity: delta, affectedPhases: PHASES.slice(),
          });
        }
      }
    }
  }

  // Sort by (t_start, kind) and re-issue sequential ids
  events.sort((a, b) => a.tStartMs - b.tStartMs || a.kind.localeCompare(b.kind));
  return events.map((ev, i) => ({ id: i, ...ev }));
}
