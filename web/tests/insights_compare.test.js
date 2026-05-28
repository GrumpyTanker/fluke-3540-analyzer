// Cross-session insights parity tests — mirror python/tests/test_insights_compare.py.
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { test } from 'node:test';

import { detectEvents } from '../events.js';
import { analyzeInsights } from '../insights.js';
import { analyzeCompareInsights } from '../insights_compare.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..', '..');
const spec = JSON.parse(readFileSync(resolve(repoRoot, 'spec', 'field_map.json'), 'utf8'));

const FI = new Map(spec.fields.map((f) => [f.name, f.index]));

function makeRecords(count, overrides = {}, startOffsetDays = 0) {
  const baseMs = Date.UTC(2024, 0, 13, 22, 0, 0) + startOffsetDays * 86_400_000;
  const base = {};
  for (const f of spec.fields) base[f.name] = 0;
  for (const ph of ['a', 'b', 'c']) {
    for (const st of ['min', 'max', 'avg']) {
      base[`V_LN_${ph}_${st}_V`] = 277;
      base[`I_${ph}_${st}_A`] = 100;
    }
  }
  base['freq_min_Hz'] = 60; base['freq_max_Hz'] = 60; base['freq_avg_Hz'] = 60;
  base['P_total_avg_W'] = 50_000;
  base['PF_total_avg'] = 0.99;
  const recs = [];
  for (let n = 0; n < count; n++) {
    const merged = { ...base, ...(overrides[n] ?? {}) };
    const floats = new Float32Array(spec.data_floats);
    for (const [name, val] of Object.entries(merged)) {
      if (FI.has(name)) floats[FI.get(name)] = val;
    }
    recs.push({ index: n, startMs: baseMs + n * 1000, endMs: baseMs + (n + 1) * 1000, floats });
  }
  return recs;
}

function plant(o, s, e, v) {
  for (let i = s; i <= e; i++) o[i] = { ...(o[i] ?? {}), ...v };
}

function session(label, recs) {
  const events = detectEvents(recs, spec, { nominalLnV: 277 });
  const findings = analyzeInsights(recs, events, spec);
  return { label, records: recs, events, findings };
}

test('compare: empty on single session', () => {
  assert.deepEqual(analyzeCompareInsights([session('only', makeRecords(120))], spec), []);
});

test('compare: voltage drift fires on rising phase B', () => {
  const sessions = [];
  for (let i = 0; i < 3; i++) {
    const ov = {};
    for (let n = 0; n < 120; n++) {
      ov[n] = { V_LN_b_min_V: 277 + i, V_LN_b_max_V: 277 + i, V_LN_b_avg_V: 277 + i };
    }
    sessions.push(session(`d${i}`, makeRecords(120, ov, i)));
  }
  const out = analyzeCompareInsights(sessions, spec);
  const b = out.find((f) => f.kind === 'voltage_drift' && f.headline.includes('B'));
  assert.ok(b);
  assert.ok(b.headline.includes('rising'));
});

test('compare: recurring outages across two captures', () => {
  const sessions = [];
  for (let i = 0; i < 2; i++) {
    const ov = {};
    plant(ov, 60, 69, {
      V_LN_a_min_V: 0, V_LN_a_max_V: 0, V_LN_a_avg_V: 0,
      V_LN_b_min_V: 0, V_LN_b_max_V: 0, V_LN_b_avg_V: 0,
      V_LN_c_min_V: 0, V_LN_c_max_V: 0, V_LN_c_avg_V: 0,
    });
    sessions.push(session(`day${i}`, makeRecords(180, ov, i)));
  }
  const out = analyzeCompareInsights(sessions, spec);
  const rec = out.find((f) => f.kind === 'recurring_outages');
  assert.ok(rec);
  assert.equal(rec.sessionLabels.length, 2);
});

test('compare: pf_degradation trending up', () => {
  const sessions = [];
  for (let i = 0; i < 3; i++) {
    const ov = {};
    const badCount = (i + 1) * 24;
    for (let n = 0; n < badCount; n++) ov[n] = { PF_total_avg: 0.55 };
    sessions.push(session(`c${i}`, makeRecords(120, ov, i * 7)));
  }
  const out = analyzeCompareInsights(sessions, spec);
  const pf = out.find((f) => f.kind === 'pf_degradation');
  assert.ok(pf);
  assert.ok(/degrading/i.test(pf.headline));
});

test('compare: event count trend fires when power steps multiply', () => {
  const sessions = [];
  for (let i = 0; i < 3; i++) {
    const ov = {};
    for (let k = 0; k < 5 * (i + 1); k++) ov[10 + k * 2] = { P_total_avg_W: 200_000 };
    sessions.push(session(`e${i}`, makeRecords(120, ov, i)));
  }
  const out = analyzeCompareInsights(sessions, spec);
  assert.ok(out.some((f) => f.kind.startsWith('event_trend_')));
});

test('compare: findings sorted by severity then kind, sequential ids', () => {
  const sessions = [];
  for (let i = 0; i < 2; i++) {
    const ov = {};
    for (let n = 0; n < 120; n++) {
      ov[n] = { V_LN_b_min_V: 277 + i * 3, V_LN_b_max_V: 277 + i * 3, V_LN_b_avg_V: 277 + i * 3 };
    }
    plant(ov, 60, 69, {
      V_LN_a_min_V: 0, V_LN_a_max_V: 0, V_LN_a_avg_V: 0,
      V_LN_b_min_V: 0, V_LN_b_max_V: 0, V_LN_b_avg_V: 0,
      V_LN_c_min_V: 0, V_LN_c_max_V: 0, V_LN_c_avg_V: 0,
    });
    sessions.push(session(`d${i}`, makeRecords(180, ov, i)));
  }
  const out = analyzeCompareInsights(sessions, spec);
  const rank = { alert: 0, warn: 1, info: 2 };
  for (let i = 1; i < out.length; i++) {
    assert.ok(rank[out[i].severity] >= rank[out[i - 1].severity]);
  }
  for (let i = 0; i < out.length; i++) assert.equal(out[i].id, i);
});
