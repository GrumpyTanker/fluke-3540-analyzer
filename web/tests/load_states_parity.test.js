// Load-state parity — web/analysis.js active/standby split, the three energy
// figures, and the magnitude-weighted reverse-CTs decision must match
// python/.../analysis.py. We recreate the exact deterministic bimodal session
// the Python golden generator builds (test_analysis_parity_golden.py
// `load_states` block) and compare against fixtures/analysis_golden.json.
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { test } from 'node:test';

import {
  classifyLoadStates, loadStateRows, sessionEnergy, activeStatePf,
  detectCtReversal,
} from '../analysis.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..', '..');
const spec = JSON.parse(readFileSync(resolve(repoRoot, 'spec', 'field_map.json'), 'utf8'));
const golden = JSON.parse(readFileSync(
  resolve(repoRoot, 'python', 'tests', 'fixtures', 'analysis_golden.json'), 'utf8'));

const FI = new Map(spec.fields.map((f) => [f.name, f.index]));
const G = golden.load_states;

// Mirror the Python `make_records` defaults + the load_states bimodal overrides.
function buildBimodalSession() {
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
  base.freq_min_Hz = 60.0; base.freq_max_Hz = 60.0; base.freq_avg_Hz = 60.0;
  base.P_total_avg_W = 50_000.0;
  base.P_total_min_W = 50_000.0; base.P_total_max_W = 50_000.0;

  const nA = G.n_active;
  const nS = G.n_standby;
  const active = {
    I_a_avg_A: 239.0, I_b_avg_A: 239.0, I_c_avg_A: 239.0,
    P_total_avg_W: 97_000.0, S_total_avg_VA: 206_000.0,
    PF_total_avg: 0.47, V_THD_pct_a_avg: 3.0, V_THD_pct_b_avg: 2.5,
    V_THD_pct_c_avg: 4.0,
  };
  const standby = {
    I_a_avg_A: 16.0, I_b_avg_A: 16.0, I_c_avg_A: 16.0,
    P_total_avg_W: -7_600.0, S_total_avg_VA: 11_800.0,
    PF_total_avg: -0.64, V_THD_pct_a_avg: 3.0, V_THD_pct_b_avg: 2.5,
    V_THD_pct_c_avg: 4.0,
  };
  const records = [];
  for (let n = 0; n < nA + nS; n++) {
    const merged = { ...base, ...(n < nA ? active : standby) };
    const floats = new Float32Array(spec.data_floats);
    for (const [name, val] of Object.entries(merged)) {
      if (FI.has(name)) floats[FI.get(name)] = val;
    }
    records.push({
      index: n,
      startMs: G.base_epoch_ms + n * 1000,
      endMs: G.base_epoch_ms + (n + 1) * 1000,
      floats,
    });
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

test('classifyLoadStates: splits by mean per-phase current at the threshold', () => {
  const recs = buildBimodalSession();
  const g = classifyLoadStates(recs, spec, { thresholdA: G.threshold_a });
  assert.equal(g.active.length, G.n_active);
  assert.equal(g.standby.length, G.n_standby);
  assert.deepEqual(g.active.slice(0, 3), [0, 1, 2]);
});

test('loadStateRows: matches Python golden row-for-row', () => {
  const recs = buildBimodalSession();
  const rows = loadStateRows(recs, spec, { thresholdA: G.threshold_a });
  const by = Object.fromEntries(rows.map((r) => [r.state, r]));
  const gby = Object.fromEntries(G.rows.map((r) => [r.state, r]));
  assert.deepEqual(Object.keys(by).sort(), Object.keys(gby).sort());
  for (const name of Object.keys(gby)) {
    const j = by[name];
    const g = gby[name];
    assert.equal(j.records, g.records, `${name}.records`);
    approx(j.hours, g.hours, 1e-6, `${name}.hours`);
    approx(j.duty_pct, g.duty_pct, 1e-6, `${name}.duty_pct`);
    approx(j.kWh, g.kWh, 1e-4, `${name}.kWh`);
    approx(j.P_avg_kW, g.P_avg_kW, 1e-3, `${name}.P_avg_kW`);
    approx(j.P_min_kW, g.P_min_kW, 1e-3, `${name}.P_min_kW`);
    approx(j.P_max_kW, g.P_max_kW, 1e-3, `${name}.P_max_kW`);
    approx(j.I_avg_A, g.I_avg_A, 1e-3, `${name}.I_avg_A`);
    approx(j.S_avg_kVA, g.S_avg_kVA, 1e-2, `${name}.S_avg_kVA`);
    approx(j.PF_avg, g.PF_avg, 1e-4, `${name}.PF_avg`);
    approx(j.V_LN_avg_V, g.V_LN_avg_V, 1e-3, `${name}.V_LN_avg_V`);
    approx(j.V_THD_p95_pct, g.V_THD_p95_pct, PCT_ABS, `${name}.V_THD_p95_pct`);
  }
});

test('sessionEnergy: three figures match Python golden', () => {
  const recs = buildBimodalSession();
  const e = sessionEnergy(recs, spec, { thresholdA: G.threshold_a });
  approx(e.energy_as_measured_kWh, G.energy.energy_as_measured_kWh, 1e-6,
    'energy_as_measured_kWh');
  approx(e.energy_active_kWh, G.energy.energy_active_kWh, 1e-6,
    'energy_active_kWh');
  approx(e.energy_net_clip_standby_kWh, G.energy.energy_net_clip_standby_kWh,
    1e-6, 'energy_net_clip_standby_kWh');
  assert.equal(e.standby_threshold_a, G.energy.standby_threshold_a);
  // The understated as-measured < the corrected figures.
  assert.ok(e.energy_as_measured_kWh < e.energy_active_kWh);
  assert.ok(e.energy_as_measured_kWh < e.energy_net_clip_standby_kWh);
});

test('activeStatePf: returns the active row PF', () => {
  const recs = buildBimodalSession();
  const rows = loadStateRows(recs, spec, { thresholdA: G.threshold_a });
  approx(activeStatePf(rows), 0.47, 1e-4, 'active PF');
});

test('detectCtReversal: magnitude-weighted decision matches Python golden', () => {
  const recs = buildBimodalSession();
  const ct = detectCtReversal(recs, spec, { activeThresholdA: G.threshold_a });
  assert.equal(ct.basis, G.ct.basis, 'basis');
  assert.equal(ct.reversed, G.ct.reversed, 'reversed');
  assert.equal(ct.active_records, G.ct.active_records, 'active_records');
  assert.equal(ct.active_negative_records, G.ct.active_negative_records,
    'active_negative_records');
  approx(ct.active_frac_negative, G.ct.active_frac_negative, 1e-9,
    'active_frac_negative');
  approx(ct.active_mean_p_w, G.ct.active_mean_p_w, 1e-3, 'active_mean_p_w');
  // The whole-session count fraction is past 50% (the fragile signal) but the
  // decision is made on the positive active state, so reversed stays false.
  approx(ct.frac_negative, G.ct.frac_negative, 1e-9, 'frac_negative');
  assert.ok(ct.frac_negative >= 0.50);
  assert.equal(ct.reversed, false);
});
