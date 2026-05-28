// Event-detection parity sanity — mirrors python/tests/test_events.py with
// the same planted-event scenarios on in-memory records.
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { test } from 'node:test';

import { detectEvents } from '../events.js';
import { pickSnapshots } from '../snapshots.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..', '..');
const spec = JSON.parse(readFileSync(resolve(repoRoot, 'spec', 'field_map.json'), 'utf8'));

// Field-name → index lookup
const FI = new Map(spec.fields.map((f) => [f.name, f.index]));

// Synthetic record builder — mirrors conftest.py's make_records()
function makeRecords(count, overrides = {}) {
  const baseMs = Date.UTC(2024, 0, 13, 22, 0, 0);
  const baseValues = {};
  for (const f of spec.fields) baseValues[f.name] = 0.0;
  for (const ph of ['a', 'b', 'c']) {
    for (const st of ['min', 'max', 'avg']) {
      baseValues[`V_LN_${ph}_${st}_V`] = 277.0;
      baseValues[`I_${ph}_${st}_A`] = 100.0;
    }
  }
  for (const pair of ['ab', 'bc', 'ca']) {
    for (const st of ['min', 'max', 'avg']) {
      baseValues[`V_LL_${pair}_${st}_V`] = 480.0;
    }
  }
  baseValues['freq_min_Hz'] = 60.0;
  baseValues['freq_max_Hz'] = 60.0;
  baseValues['freq_avg_Hz'] = 60.0;
  baseValues['P_total_avg_W'] = 50_000.0;
  baseValues['P_total_min_W'] = 50_000.0;
  baseValues['P_total_max_W'] = 50_000.0;

  const records = [];
  for (let n = 0; n < count; n++) {
    const merged = { ...baseValues, ...(overrides[n] ?? {}) };
    const floats = new Float32Array(spec.data_floats);
    for (const [name, val] of Object.entries(merged)) {
      if (FI.has(name)) floats[FI.get(name)] = val;
    }
    records.push({
      index: n,
      startMs: baseMs + n * 1000,
      endMs: baseMs + (n + 1) * 1000,
      floats,
    });
  }
  return records;
}

function plantWindow(overrides, start, end, values) {
  for (let i = start; i <= end; i++) {
    overrides[i] = { ...(overrides[i] ?? {}), ...values };
  }
}

test('events: flat healthy data yields no events', () => {
  const events = detectEvents(makeRecords(120), spec);
  assert.equal(events.length, 0);
});

test('events: outage detected', () => {
  const overrides = {};
  plantWindow(overrides, 60, 69, {
    V_LN_a_min_V: 0, V_LN_a_max_V: 0, V_LN_a_avg_V: 0,
    V_LN_b_min_V: 0, V_LN_b_max_V: 0, V_LN_b_avg_V: 0,
    V_LN_c_min_V: 0, V_LN_c_max_V: 0, V_LN_c_avg_V: 0,
  });
  const events = detectEvents(makeRecords(180, overrides), spec, { nominalLnV: 277.0 });
  const outages = events.filter((e) => e.kind === 'outage');
  assert.equal(outages.length, 1);
  assert.equal(outages[0].severity, 0);
});

test('events: dip on phase a', () => {
  const overrides = {};
  plantWindow(overrides, 30, 34, { V_LN_a_min_V: 200.0 });
  const events = detectEvents(makeRecords(120, overrides), spec, { nominalLnV: 277.0 });
  const dips = events.filter((e) => e.kind === 'dip');
  assert.equal(dips.length, 1);
  assert.deepEqual(dips[0].affectedPhases, ['a']);
  assert.ok(Math.abs(dips[0].severity - 200.0 / 277.0) < 1e-3);
});

test('events: swell on phase b', () => {
  const overrides = {};
  plantWindow(overrides, 50, 52, { V_LN_b_max_V: 320.0 });
  const events = detectEvents(makeRecords(120, overrides), spec, { nominalLnV: 277.0 });
  const swells = events.filter((e) => e.kind === 'swell');
  assert.equal(swells.length, 1);
  assert.deepEqual(swells[0].affectedPhases, ['b']);
});

test('events: high current on phase c', () => {
  const overrides = {};
  overrides[80] = { I_c_max_A: 500.0 };
  const events = detectEvents(makeRecords(200, overrides), spec, { nominalLnV: 277.0 });
  const spikes = events.filter((e) => e.kind === 'high_current');
  assert.ok(spikes.length >= 1, 'expected at least one high_current event');
  const sc = spikes.find((e) => e.affectedPhases.includes('c'));
  assert.equal(sc.severity, 500.0);
});

test('events: frequency excursion', () => {
  const overrides = {};
  plantWindow(overrides, 40, 41, { freq_avg_Hz: 60.7 });
  const events = detectEvents(makeRecords(120, overrides), spec, { nominalLnV: 277.0 });
  const fe = events.filter((e) => e.kind === 'freq_excursion');
  assert.equal(fe.length, 1);
  assert.ok(Math.abs(fe[0].severity - 0.7) < 1e-3);
});

test('events: imbalance spike', () => {
  const overrides = {};
  plantWindow(overrides, 30, 34, { V_LN_c_avg_V: 260.0 });
  const events = detectEvents(makeRecords(120, overrides), spec, { nominalLnV: 277.0 });
  const imb = events.filter((e) => e.kind === 'imbalance_spike');
  assert.equal(imb.length, 1);
  assert.ok(imb[0].severity > 2.5);
});

test('events: power step', () => {
  const overrides = {};
  for (let i = 60; i < 120; i++) overrides[i] = { P_total_avg_W: 200_000.0 };
  const events = detectEvents(makeRecords(120, overrides), spec, { nominalLnV: 277.0 });
  const steps = events.filter((e) => e.kind === 'power_step');
  assert.equal(steps.length, 1);
  assert.ok(steps[0].severity > 0);
});

test('events: nominal voltage auto-inference', () => {
  const overrides = {};
  plantWindow(overrides, 50, 59, {
    V_LN_a_avg_V: 0, V_LN_b_avg_V: 0, V_LN_c_avg_V: 0,
    V_LN_a_min_V: 0, V_LN_b_min_V: 0, V_LN_c_min_V: 0,
  });
  const events = detectEvents(makeRecords(200, overrides), spec);
  const kinds = new Set(events.map((e) => e.kind));
  assert.deepEqual([...kinds], ['outage']);
});

test('events: sequential ids in time order', () => {
  const overrides = {};
  plantWindow(overrides, 10, 12, { V_LN_a_max_V: 320.0 });  // earlier swell
  plantWindow(overrides, 80, 84, { V_LN_b_min_V: 200.0 });  // later dip
  const events = detectEvents(makeRecords(200, overrides), spec, { nominalLnV: 277.0 });
  for (let i = 0; i < events.length; i++) assert.equal(events[i].id, i);
  assert.ok(events[0].tStartMs < events[events.length - 1].tStartMs);
});

test('snapshots: empty input returns empty', () => {
  assert.deepEqual(pickSnapshots([], [], spec), []);
});

test('snapshots: picks calmest non-event window', () => {
  const overrides = {};
  for (let i = 0; i < 600; i++) {
    overrides[i] = { P_total_avg_W: 50_000.0 + (i % 7) * 10_000.0 };
  }
  const records = makeRecords(1200, overrides);
  const snaps = pickSnapshots(records, [], spec,
                              { n: 1, windowSecs: 300, minSeparationSecs: 1 });
  assert.equal(snaps.length, 1);
  // Quietest window should fall in the second half
  assert.ok(snaps[0].tCenterMs >= records[600].startMs);
});
