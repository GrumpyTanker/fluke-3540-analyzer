// Feature B parity — web/analysis.js must produce the same numbers as
// python/.../analysis.py. We recreate the exact deterministic session the
// Python golden generator builds (test_analysis_parity_golden.py), run the JS
// analysis, and compare against fixtures/analysis_golden.json.
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { test } from 'node:test';

import {
  wholeSessionStats, classifyItic, eventItic, timeOfDayProfile,
  detectCtReversal, ctReversalNotice,
} from '../analysis.js';
import { buildNarrative, narrativeMarkdown } from '../narrative.js';
import { ieee519Compliance, sarfiIndices, demandAnalysis } from '../analysis.js';
import { ColumnStore } from '../column_store.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..', '..');
const spec = JSON.parse(readFileSync(resolve(repoRoot, 'spec', 'field_map.json'), 'utf8'));
const golden = JSON.parse(readFileSync(
  resolve(repoRoot, 'python', 'tests', 'fixtures', 'analysis_golden.json'), 'utf8'));

const FI = new Map(spec.fields.map((f) => [f.name, f.index]));
const baseMs = Date.UTC(2024, 0, 13, 22, 0, 0);

// Mirror conftest.make_records defaults + the golden generator's overrides.
function buildSession() {
  const base = {};
  for (const f of spec.fields) base[f.name] = 0.0;
  for (const ph of ['a', 'b', 'c']) {
    for (const st of ['min', 'max', 'avg']) {
      base[`V_LN_${ph}_${st}_V`] = 277.0;
      base[`I_${ph}_${st}_A`] = 100.0;
    }
  }
  for (const pair of ['ab', 'bc', 'ca']) {
    for (const st of ['min', 'max', 'avg']) base[`V_LL_${pair}_${st}_V`] = 480.0;
  }
  base['freq_min_Hz'] = 60.0; base['freq_max_Hz'] = 60.0; base['freq_avg_Hz'] = 60.0;
  base['P_total_avg_W'] = 50_000.0;
  base['P_total_min_W'] = 50_000.0; base['P_total_max_W'] = 50_000.0;

  const overrides = {};
  for (let i = 0; i < 600; i++) {
    overrides[i] = {
      P_total_avg_W: 50_000.0 + (i % 50) * 1000.0,
      V_LN_a_avg_V: 277.0 + (i % 7) - 3.0,
      I_a_avg_A: 100.0 + (i % 11),
      PF_total_avg: 0.90 + (i % 5) * 0.01,
      freq_avg_Hz: 60.0 + ((i % 3) - 1) * 0.01,
      V_THD_pct_a_avg: 3.0 + (i % 13) * 0.5,
      V_THD_pct_b_avg: 2.0 + (i % 5) * 0.2,
      V_THD_pct_c_avg: 4.5 + (i % 3) * 0.1,
      I_THD_pct_a_avg: 8.0 + (i % 7),
    };
  }
  for (let i = 100; i <= 119; i++) overrides[i].V_LN_a_avg_V = 240.0;
  for (let i = 200; i <= 204; i++) overrides[i].I_c_avg_A = 850.0;

  const records = [];
  for (let n = 0; n < 600; n++) {
    const merged = { ...base, ...(overrides[n] ?? {}) };
    const floats = new Float32Array(spec.data_floats);
    for (const [name, val] of Object.entries(merged)) {
      if (FI.has(name)) floats[FI.get(name)] = val;
    }
    records.push({ index: n, startMs: baseMs + n * 1000, endMs: baseMs + (n + 1) * 1000, floats });
  }
  return records;
}

const REL = 1e-4;       // relative tolerance for means/stdev
const PCT_ABS = 1.0;    // percentile sketch is bin-width bounded; allow ~1 unit

function approx(actual, expected, absTol, msg) {
  if (expected === null || actual === null) { assert.equal(actual, expected, msg); return; }
  const tol = Math.max(absTol, Math.abs(expected) * REL);
  assert.ok(Math.abs(actual - expected) <= tol,
    `${msg}: got ${actual}, expected ${expected} (tol ${tol})`);
}

test('wholeSessionStats: matches Python golden within tolerance', () => {
  const stats = wholeSessionStats(buildSession(), spec);
  for (const [name, g] of Object.entries(golden.stats)) {
    if (name.startsWith('_')) continue;
    const js = stats[name];
    assert.ok(js, `JS stats missing channel ${name}`);
    assert.equal(js.count, g.count, `${name}.count`);
    assert.equal(js.unit, g.unit, `${name}.unit`);
    approx(js.mean, g.mean, 1e-3, `${name}.mean`);
    approx(js.stdev, g.stdev, 1e-3, `${name}.stdev`);
    approx(js.min, g.min, 1e-3, `${name}.min`);
    approx(js.max, g.max, 1e-3, `${name}.max`);
    // Percentiles share the identical fixed-width sketch, so they match closely.
    for (const q of ['p1', 'p5', 'median', 'p95', 'p99']) {
      approx(js[q], g[q], PCT_ABS, `${name}.${q}`);
    }
  }
  // Threshold accounting must match exactly.
  const t = stats._thresholds;
  const gt = golden.stats._thresholds;
  assert.equal(t.sec_undervoltage, gt.sec_undervoltage);
  assert.equal(t.sec_overcurrent, gt.sec_overcurrent);
  assert.equal(t.total_records, gt.total_records);
  approx(t.pct_undervoltage, gt.pct_undervoltage, 1e-6, 'pct_undervoltage');
});

test('classifyItic: matches Python golden exactly', () => {
  for (let i = 0; i < golden.itic_points.length; i++) {
    const [residual, dur] = golden.itic_points[i];
    assert.equal(classifyItic(residual, dur), golden.itic[i],
      `itic point ${i} (${residual}%, ${dur}s)`);
  }
});

test('eventItic: matches Python golden', () => {
  const base = baseMs;
  const evs = [
    { kind: 'dip', tStartMs: base, tEndMs: base + 2000, severity: 0.72 },
    { kind: 'outage', tStartMs: base, tEndMs: base + 120000, severity: 0.0 },
    { kind: 'swell', tStartMs: base, tEndMs: base + 1000, severity: 1.14 },
  ];
  for (let i = 0; i < evs.length; i++) {
    const js = eventItic(evs[i], 277.0);
    const g = golden.event_itic[i];
    approx(js.residual_pct, g.residual_pct, 1e-4, `event_itic[${i}].residual_pct`);
    approx(js.duration_secs, g.duration_secs, 1e-9, `event_itic[${i}].duration_secs`);
    assert.equal(js.itic_class, g.itic_class, `event_itic[${i}].itic_class`);
  }
});

test('detectCtReversal: matches Python golden (mixed 70% negative session)', () => {
  // Recreate the golden CT session: 70 records P=-30kW, 30 records P=+30kW.
  const base = {};
  for (const f of spec.fields) base[f.name] = 0.0;
  for (const ph of ['a', 'b', 'c']) {
    for (const st of ['min', 'max', 'avg']) {
      base[`V_LN_${ph}_${st}_V`] = 277.0;
      base[`I_${ph}_${st}_A`] = 100.0;
    }
  }
  base['freq_avg_Hz'] = 60.0;
  const records = [];
  for (let n = 0; n < 100; n++) {
    const merged = { ...base, P_total_avg_W: n < 70 ? -30000.0 : 30000.0 };
    const floats = new Float32Array(spec.data_floats);
    for (const [name, val] of Object.entries(merged)) {
      if (FI.has(name)) floats[FI.get(name)] = val;
    }
    records.push({ index: n, startMs: baseMs + n * 1000, endMs: baseMs + (n + 1) * 1000, floats });
  }
  const js = detectCtReversal(records, spec);
  const g = golden.ct_reversal;
  assert.equal(js.reversed, g.reversed);
  assert.equal(js.non_outage_records, g.non_outage_records);
  assert.equal(js.negative_records, g.negative_records);
  approx(js.frac_negative, g.frac_negative, 1e-9, 'frac_negative');
  approx(js.mean_p_w, g.mean_p_w, 1e-3, 'mean_p_w');
  // Notice is loud + actionable.
  const notice = ctReversalNotice(js);
  assert.match(notice, /CT REVERSAL DETECTED/);
  assert.match(notice, /Reverse CTs/);
});

test('buildNarrative: matches Python golden exactly', () => {
  const nb = baseMs;
  const events = [
    { id: 0, kind: 'dip', tStartMs: nb + 90000, tEndMs: nb + 92000, severity: 0.72, affectedPhases: ['c'] },
    { id: 1, kind: 'outage', tStartMs: nb + 100000, tEndMs: nb + 1210000, severity: 0.0, affectedPhases: ['a', 'b', 'c'] },
  ];
  const findings = [
    { id: 0, kind: 'pf_drift', severity: 'alert', headline: 'Power factor below 0.85 for 99.8% of non-outage time' },
  ];
  const stats = {
    PF_total_avg: { mean: 0.81, p5: 0.70, p95: 0.95 },
    _thresholds: { total_records: 590000 },
  };
  const ct = { reversed: true, frac_negative: 0.52 };
  const js = buildNarrative(events, findings, stats, ct, {
    config: { asset_name: 'MAC03' }, totalRecords: 590000, durationSecs: 604800.0,
  });
  assert.equal(js, golden.narrative);
});

test('ieee519Compliance: matches Python golden', () => {
  const res = ieee519Compliance(buildSession(), spec);
  const g = golden.ieee519;
  assert.equal(res.all_voltage_compliant, g.all_voltage_compliant);
  for (const ph of ['a', 'b', 'c']) {
    approx(res.voltage[ph].p95, g.voltage[ph].p95, PCT_ABS, `V_THD ${ph} p95`);
    assert.equal(res.voltage[ph].compliant, g.voltage[ph].compliant, `${ph} compliant`);
    assert.equal(res.voltage[ph].exceeds_planning, g.voltage[ph].exceeds_planning, `${ph} planning`);
    approx(res.current[ph].p95, g.current[ph].p95, PCT_ABS, `I_THD ${ph} p95`);
  }
});

test('sarfiIndices: matches Python golden', () => {
  // Same sample events the golden uses: dip 72%, outage 0%, swell (ignored).
  const events = [
    { kind: 'dip', tStartMs: baseMs, tEndMs: baseMs + 2000, severity: 0.72 },
    { kind: 'outage', tStartMs: baseMs, tEndMs: baseMs + 120000, severity: 0.0 },
    { kind: 'swell', tStartMs: baseMs, tEndMs: baseMs + 1000, severity: 1.14 },
  ];
  const res = sarfiIndices(events, 277.0);
  const g = golden.sarfi;
  for (const k of ['SARFI-90', 'SARFI-80', 'SARFI-70', 'SARFI-50', 'SARFI-10', 'events_considered']) {
    assert.equal(res[k], g[k], k);
  }
});

test('demandAnalysis: matches Python golden (ramp, 120s window)', () => {
  // Recreate the ramp store: P = i*100 W over 600 records.
  const records = [];
  for (let n = 0; n < 600; n++) {
    const floats = new Float32Array(spec.data_floats);
    for (const ph of ['a', 'b', 'c']) {
      for (const st of ['min', 'max', 'avg']) {
        floats[FI.get(`V_LN_${ph}_${st}_V`)] = 277.0;
        floats[FI.get(`I_${ph}_${st}_A`)] = 100.0;
      }
    }
    floats[FI.get('freq_avg_Hz')] = 60.0;
    floats[FI.get('P_total_avg_W')] = n * 100.0;
    records.push({ index: n, startMs: baseMs + n * 1000, endMs: baseMs + (n + 1) * 1000, floats });
  }
  const store = ColumnStore.fromRecords(records, spec);
  const res = demandAnalysis(store, spec, { windowSecs: 120, seriesStepSecs: 120 });
  const g = golden.demand;
  assert.equal(res.window_secs, g.window_secs);
  assert.equal(res.n_windows, g.n_windows);
  approx(res.peak_demand_w, g.peak_demand_w, 1e-3, 'peak_demand_w');
  approx(res.peak_demand_kw, g.peak_demand_kw, 1e-6, 'peak_demand_kw');
  // Timestamps are the same instant; compare as epoch ms (JS emits Z, Python +00:00).
  assert.equal(Date.parse(res.peak_window_end), Date.parse(g.peak_window_end));
  assert.equal(Date.parse(res.peak_window_start), Date.parse(g.peak_window_start));
  assert.equal(res.series.length, g.series.length);
  for (let i = 0; i < res.series.length; i++) {
    assert.equal(Date.parse(res.series[i].t), Date.parse(g.series[i].t), `series[${i}].t`);
    approx(res.series[i].demand_w, g.series[i].demand_w, 1e-3, `series[${i}].demand_w`);
  }
});

test('narrativeMarkdown: wraps with a heading', () => {
  const md = narrativeMarkdown('Hello.', { asset_name: 'ABC' });
  assert.match(md, /^# Executive Summary — ABC/);
  assert.match(md, /Hello\./);
});

test('timeOfDayProfile: matches Python golden row-for-row', () => {
  const rows = timeOfDayProfile(buildSession(), spec, { window: [0, 1440], binMinutes: 1 });
  assert.equal(rows.length, golden.tod_rows.length, 'row count');
  for (let i = 0; i < rows.length; i++) {
    const js = rows[i];
    const g = golden.tod_rows[i];
    assert.equal(js.bin, g.bin, `row ${i} bin`);
    assert.equal(js.n, g.n, `row ${i} n`);
    assert.equal(js.n_days, g.n_days, `row ${i} n_days`);
    approx(js.p_avg_kW, g.p_avg_kW, 1e-3, `row ${i} p_avg_kW`);
    approx(js.v_avg_V, g.v_avg_V, 1e-3, `row ${i} v_avg_V`);
    approx(js.i_avg_A, g.i_avg_A, 1e-3, `row ${i} i_avg_A`);
  }
});
