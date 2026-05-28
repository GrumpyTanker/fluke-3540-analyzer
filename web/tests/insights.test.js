// Parity tests for the Insights engine — same planted scenarios as the
// Python test_insights.py.
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { test } from 'node:test';

import { detectEvents } from '../events.js';
import { analyzeInsights } from '../insights.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..', '..');
const spec = JSON.parse(readFileSync(resolve(repoRoot, 'spec', 'field_map.json'), 'utf8'));

const FI = new Map(spec.fields.map((f) => [f.name, f.index]));

function makeRecords(count, overrides = {}, baseMs = Date.UTC(2024, 0, 13, 22, 0, 0)) {
  const base = {};
  for (const f of spec.fields) base[f.name] = 0.0;
  for (const ph of ['a', 'b', 'c']) {
    for (const st of ['min', 'max', 'avg']) {
      base[`V_LN_${ph}_${st}_V`] = 277.0;
      base[`I_${ph}_${st}_A`] = 100.0;
    }
  }
  base['freq_min_Hz'] = 60.0; base['freq_max_Hz'] = 60.0; base['freq_avg_Hz'] = 60.0;
  base['P_total_avg_W'] = 50_000.0;
  base['PF_total_avg'] = 0.99;
  const recs = [];
  for (let n = 0; n < count; n++) {
    const merged = { ...base, ...(overrides[n] ?? {}) };
    const floats = new Float32Array(spec.data_floats);
    for (const [name, val] of Object.entries(merged)) {
      if (FI.has(name)) floats[FI.get(name)] = val;
    }
    recs.push({
      index: n,
      startMs: baseMs + n * 1000,
      endMs: baseMs + (n + 1) * 1000,
      floats,
    });
  }
  return recs;
}

function plant(overrides, start, end, values) {
  for (let i = start; i <= end; i++) {
    overrides[i] = { ...(overrides[i] ?? {}), ...values };
  }
}

test('insights: empty on healthy data', () => {
  const f = analyzeInsights(makeRecords(120), [], spec);
  assert.equal(f.length, 0);
});

test('insights: phase asymmetry warn', () => {
  const overrides = {};
  for (let i = 0; i < 120; i++) {
    overrides[i] = {
      V_LN_b_min_V: 285, V_LN_b_max_V: 285, V_LN_b_avg_V: 285,
    };
  }
  const f = analyzeInsights(makeRecords(120, overrides), [], spec);
  const pa = f.find((x) => x.kind === 'phase_asymmetry');
  assert.ok(pa, 'expected phase_asymmetry finding');
  assert.ok(pa.severity === 'warn' || pa.severity === 'alert');
  assert.ok(/B/.test(pa.headline));
});

test('insights: outage signature with leading dip', () => {
  const overrides = {};
  plant(overrides, 58, 59, { V_LN_a_min_V: 200 });
  plant(overrides, 60, 69, {
    V_LN_a_min_V: 0, V_LN_a_max_V: 0, V_LN_a_avg_V: 0,
    V_LN_b_min_V: 0, V_LN_b_max_V: 0, V_LN_b_avg_V: 0,
    V_LN_c_min_V: 0, V_LN_c_max_V: 0, V_LN_c_avg_V: 0,
  });
  const recs = makeRecords(180, overrides);
  const events = detectEvents(recs, spec, { nominalLnV: 277.0 });
  const f = analyzeInsights(recs, events, spec);
  const sig = f.find((x) => x.kind === 'outage_signature');
  assert.ok(sig);
  assert.ok(sig.relatedEventIds.length >= 2);
  assert.ok(sig.detail.includes('leading dip'));
});

test('insights: pf_drift recommends kvar sizing', () => {
  const overrides = {};
  for (let i = 40; i < 80; i++) {
    overrides[i] = {
      PF_total_avg: 0.60,
      Q_total_avg_VAR: 30000,
      S_total_avg_VA: 80000,
    };
  }
  const f = analyzeInsights(makeRecords(120, overrides), [], spec);
  const pf = f.find((x) => x.kind === 'pf_drift');
  assert.ok(pf);
  assert.ok(pf.headline.includes('below 0.85'));
  assert.ok(pf.recommendedActions.some((a) => /kVAR/.test(a)));
});

test('insights: breaker context upgrades spike severity', () => {
  // Phase C peaks at 800 A on top of an otherwise-100 A baseline → ratio = 8×
  const overrides = {};
  for (let i = 0; i < 200; i++) overrides[i] = { I_c_max_A: 100 };
  overrides[100] = { I_c_max_A: 800 };
  const recs = makeRecords(200, overrides);
  // Without breaker context — fires as info on ratio alone
  const noCtx = analyzeInsights(recs, [], spec, [], null, {});
  const cInfo = noCtx.find((f) => f.kind === 'current_spike_ratio' && f.headline.includes('C'));
  assert.ok(cInfo);
  assert.equal(cInfo.severity, 'info');
  // With breaker = 500 A — 800/500 = 160% → alert + breaker_margin finding fires
  const withCtx = analyzeInsights(recs, [], spec, [], null, { breakerRatingA: 500 });
  const cAlert = withCtx.find((f) => f.kind === 'current_spike_ratio' && f.headline.includes('C'));
  assert.ok(cAlert);
  assert.equal(cAlert.severity, 'alert');
  assert.ok(/breaker rating/i.test(cAlert.headline));
  const margin = withCtx.find((f) => f.kind === 'breaker_margin');
  assert.ok(margin);
  assert.equal(margin.severity, 'alert');
});

test('insights: breaker context warns when 80-100% of rating', () => {
  const overrides = {};
  for (let i = 0; i < 200; i++) overrides[i] = { I_a_max_A: 50 };
  overrides[100] = { I_a_max_A: 350 };  // 87.5% of 400 A breaker
  const recs = makeRecords(200, overrides);
  const out = analyzeInsights(recs, [], spec, [], null, { breakerRatingA: 400 });
  const f = out.find((x) => x.kind === 'current_spike_ratio' && x.headline.includes('A'));
  assert.ok(f);
  assert.equal(f.severity, 'warn');
  assert.equal(out.find((x) => x.kind === 'breaker_margin'), undefined);
});

test('insights: sorted alert > warn > info', () => {
  const overrides = {};
  for (let i = 0; i < 120; i++) {
    overrides[i] = {
      V_LN_b_avg_V: 290, V_LN_b_min_V: 290, V_LN_b_max_V: 290,
      PF_total_avg: 0.60, Q_total_avg_VAR: 30000, S_total_avg_VA: 80000,
    };
  }
  const f = analyzeInsights(makeRecords(120, overrides), [], spec);
  const rank = { alert: 0, warn: 1, info: 2 };
  for (let i = 1; i < f.length; i++) {
    assert.ok(rank[f[i].severity] >= rank[f[i - 1].severity]);
  }
  for (let i = 0; i < f.length; i++) assert.equal(f[i].id, i);
});

test('insights: outage_frequency triggers above threshold', () => {
  const overrides = {};
  plant(overrides, 1000, 1009, {
    V_LN_a_min_V: 0, V_LN_a_max_V: 0, V_LN_a_avg_V: 0,
    V_LN_b_min_V: 0, V_LN_b_max_V: 0, V_LN_b_avg_V: 0,
    V_LN_c_min_V: 0, V_LN_c_max_V: 0, V_LN_c_avg_V: 0,
  });
  plant(overrides, 50000, 50009, {
    V_LN_a_min_V: 0, V_LN_a_max_V: 0, V_LN_a_avg_V: 0,
    V_LN_b_min_V: 0, V_LN_b_max_V: 0, V_LN_b_avg_V: 0,
    V_LN_c_min_V: 0, V_LN_c_max_V: 0, V_LN_c_avg_V: 0,
  });
  const recs = makeRecords(86400, overrides);
  const events = detectEvents(recs, spec, { nominalLnV: 277.0 });
  const f = analyzeInsights(recs, events, spec);
  const of_ = f.find((x) => x.kind === 'outage_frequency');
  assert.ok(of_);
  assert.ok(of_.headline.includes('/day'));
});
