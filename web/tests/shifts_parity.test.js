// Shifts parity — web/analysis.js shift functions must produce the same
// numbers as python/.../analysis.py. We recreate the exact deterministic
// minute-resolution multi-day session the Python golden generator builds
// (test_analysis_parity_golden.py `shifts` block), run the JS shift analysis,
// and compare against fixtures/analysis_golden.json["shifts"].
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { test } from 'node:test';

import {
  ShiftSet, aggregateShifts, shiftOccurrences, shiftComparisonRows,
} from '../analysis.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..', '..');
const spec = JSON.parse(readFileSync(resolve(repoRoot, 'spec', 'field_map.json'), 'utf8'));
const golden = JSON.parse(readFileSync(
  resolve(repoRoot, 'python', 'tests', 'fixtures', 'analysis_golden.json'), 'utf8'));

const FI = new Map(spec.fields.map((f) => [f.name, f.index]));
const G = golden.shifts;

// Mirror the Python `shift_recs` builder exactly.
function buildShiftSession() {
  const baseMs = G.base_epoch_ms;
  const nMin = G.n_records;
  const records = [];
  for (let n = 0; n < nMin; n++) {
    const startMs = baseMs + n * 60000;
    const mod = n % 1440;
    const pW = 30000.0 + 5000.0 * ((mod >= 360 && mod < 1080) ? 1 : 0) + (n % 97) * 50.0;
    const v = 277.0 + ((n % 11) - 5) * 0.4;
    const floats = new Float32Array(spec.data_floats);
    for (const ph of ['a', 'b', 'c']) {
      floats[FI.get(`V_LN_${ph}_avg_V`)] = v;
      floats[FI.get(`I_${ph}_avg_A`)] = 100.0 + (n % 13);
    }
    floats[FI.get('P_total_avg_W')] = pW;
    floats[FI.get('PF_total_avg')] = 0.92 + (n % 5) * 0.01;
    floats[FI.get('V_THD_pct_a_avg')] = 3.0 + (n % 7) * 0.3;
    floats[FI.get('V_THD_pct_b_avg')] = 2.5 + (n % 5) * 0.2;
    floats[FI.get('V_THD_pct_c_avg')] = 4.0 + (n % 3) * 0.25;
    records.push({ index: n, startMs, endMs: startMs + 60000, floats });
  }
  return records;
}

const REL = 1e-4;
const PCT_ABS = 1.0;  // percentile sketch is bin-width bounded

function approx(actual, expected, absTol, msg) {
  const tol = Math.max(absTol, Math.abs(expected) * REL);
  assert.ok(Math.abs(actual - expected) <= tol,
    `${msg}: got ${actual}, expected ${expected} (tol ${tol})`);
}

test('aggregateShifts: index grouping matches occurrences (UTC)', () => {
  const recs = buildShiftSession();
  const ss = ShiftSet.parse('day=06:00-18:00,night=18:00-06:00');
  const by = aggregateShifts(recs, spec, ss, { tz: null });
  // day total must equal sum of day-occurrence lengths from the golden.
  const dayLen = G.occurrences_utc.filter((o) => o.name === 'day')
    .reduce((s, o) => s + (o.hi - o.lo), 0);
  const nightLen = G.occurrences_utc.filter((o) => o.name === 'night')
    .reduce((s, o) => s + (o.hi - o.lo), 0);
  assert.equal(by.day.length, dayLen);
  assert.equal(by.night.length, nightLen);
});

test('shiftOccurrences: matches Python golden row-for-row (UTC)', () => {
  const recs = buildShiftSession();
  const ss = ShiftSet.parse('day=06:00-18:00,night=18:00-06:00');
  const occ = shiftOccurrences(recs, spec, ss, { tz: null });
  assert.equal(occ.length, G.occurrences_utc.length, 'occurrence count');
  for (let i = 0; i < occ.length; i++) {
    const js = occ[i];
    const g = G.occurrences_utc[i];
    assert.equal(js.label, g.label, `occ[${i}].label`);
    assert.equal(js.name, g.name, `occ[${i}].name`);
    assert.equal(js.lo, g.lo, `occ[${i}].lo`);
    assert.equal(js.hi, g.hi, `occ[${i}].hi`);
  }
});

test('shiftComparisonRows: matches Python golden (UTC, day vs night)', () => {
  const recs = buildShiftSession();
  const ss = ShiftSet.parse('day=06:00-18:00,night=18:00-06:00');
  const rows = shiftComparisonRows(recs, spec, ss,
    { tz: null, events: [], demandWindow: 15 });
  const by = Object.fromEntries(rows.map((r) => [r.shift, r]));
  const gby = Object.fromEntries(G.comparison_utc.map((r) => [r.shift, r]));
  assert.deepEqual(Object.keys(by).sort(), Object.keys(gby).sort());
  for (const name of Object.keys(gby)) {
    const j = by[name];
    const g = gby[name];
    assert.equal(j.window, g.window, `${name}.window`);
    assert.equal(j.records, g.records, `${name}.records`);
    approx(j.hours, g.hours, 1e-6, `${name}.hours`);
    approx(j.kWh, g.kWh, 1e-3, `${name}.kWh`);
    approx(j.P_total_avg_W, g.P_total_avg_W, 1e-3, `${name}.P_total_avg_W`);
    approx(j.P_total_min_W, g.P_total_min_W, 1e-3, `${name}.P_total_min_W`);
    approx(j.P_total_max_W, g.P_total_max_W, 1e-3, `${name}.P_total_max_W`);
    approx(j.peak_demand_kW, g.peak_demand_kW, 1e-3, `${name}.peak_demand_kW`);
    approx(j.PF_avg, g.PF_avg, 1e-4, `${name}.PF_avg`);
    approx(j.V_LN_avg_V, g.V_LN_avg_V, 1e-3, `${name}.V_LN_avg_V`);
    approx(j.V_LN_p5_V, g.V_LN_p5_V, PCT_ABS, `${name}.V_LN_p5_V`);
    approx(j.V_LN_p95_V, g.V_LN_p95_V, PCT_ABS, `${name}.V_LN_p95_V`);
    approx(j.V_THD_p95_pct, g.V_THD_p95_pct, PCT_ABS, `${name}.V_THD_p95_pct`);
    assert.equal(j.n_outages, g.n_outages, `${name}.n_outages`);
    assert.equal(j.n_dips, g.n_dips, `${name}.n_dips`);
    assert.equal(j.n_swells, g.n_swells, `${name}.n_swells`);
    approx(j.outage_minutes, g.outage_minutes, 1e-6, `${name}.outage_minutes`);
  }
});

test('shiftComparisonRows: tz-localized A/B/C matches Python golden (America/Chicago)', () => {
  const recs = buildShiftSession();
  const ss = ShiftSet.parse('A=06:00-14:00,B=14:00-22:00,C=22:00-06:00');
  const rows = shiftComparisonRows(recs, spec, ss,
    { tz: 'America/Chicago', events: [], demandWindow: 15 });
  const by = Object.fromEntries(rows.map((r) => [r.shift, r]));
  const gby = Object.fromEntries(G.comparison_abc_chicago.map((r) => [r.shift, r]));
  assert.deepEqual(Object.keys(by).sort(), Object.keys(gby).sort());
  for (const name of Object.keys(gby)) {
    assert.equal(by[name].records, gby[name].records, `${name}.records (tz)`);
    approx(by[name].P_total_avg_W, gby[name].P_total_avg_W, 1e-3, `${name}.P_avg (tz)`);
    approx(by[name].kWh, gby[name].kWh, 1e-3, `${name}.kWh (tz)`);
  }
});

test('ShiftSet.parse: rejects bad/duplicate/zero-length specs', () => {
  assert.throws(() => ShiftSet.parse('day=06:00-25:00'));
  assert.throws(() => ShiftSet.parse('day=0600-1800'));
  assert.throws(() => ShiftSet.parse('garbage'));
  assert.throws(() => ShiftSet.parse('day=06:00-12:00,day=12:00-18:00'));
  assert.throws(() => ShiftSet.parse('x=06:00-06:00'));
});

test('ShiftSet.coverageIssues: clean tiling vs gap vs overlap', () => {
  assert.deepEqual(ShiftSet.parse('day=06:00-18:00,night=18:00-06:00').coverageIssues(), []);
  assert.ok(ShiftSet.parse('m=06:00-12:00,e=14:00-20:00').coverageIssues()
    .some((s) => s.toLowerCase().includes('gap')));
  assert.ok(ShiftSet.parse('a=06:00-13:00,b=12:00-18:00').coverageIssues()
    .some((s) => s.toLowerCase().includes('overlap')));
});
