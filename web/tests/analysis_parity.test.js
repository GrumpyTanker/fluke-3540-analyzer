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
} from '../analysis.js';

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
