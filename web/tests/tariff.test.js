// Tariff calculator parity tests — mirror python/tests/test_tariff.py.
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { test } from 'node:test';

import {
  computeCost, isPeak, normalizeTariff,
  parsePeakHoursString, peakHoursToString,
} from '../tariff.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const spec = JSON.parse(readFileSync(
  resolve(__dirname, '..', '..', 'spec', 'field_map.json'), 'utf8'));

const WH_IDX = spec.fields.find((f) => f.name === 'Wh_total').index;

function record(hourUtc, whTotal) {
  const baseMs = Date.UTC(2024, 0, 13, 0, 0, 0);
  const floats = new Float32Array(spec.data_floats);
  floats[WH_IDX] = whTotal;
  return {
    index: 0,
    startMs: baseMs + hourUtc * 3_600_000,
    endMs: baseMs + hourUtc * 3_600_000 + 1000,
    floats,
  };
}

test('isPeak: simple range', () => {
  const t = normalizeTariff({ peakHours: [[9, 21]] });
  assert.equal(isPeak(t, 8), false);
  assert.equal(isPeak(t, 9), true);
  assert.equal(isPeak(t, 20), true);
  assert.equal(isPeak(t, 21), false);
});

test('isPeak: wraps midnight', () => {
  const t = normalizeTariff({ peakHours: [[22, 6]] });
  assert.equal(isPeak(t, 23), true);
  assert.equal(isPeak(t, 0), true);
  assert.equal(isPeak(t, 5), true);
  assert.equal(isPeak(t, 6), false);
  assert.equal(isPeak(t, 12), false);
});

test('computeCost: flat off-peak only', () => {
  const t = normalizeTariff({ currency: 'USD', offpeakRate: 0.10 });
  const recs = [];
  for (let h = 0; h < 10; h++) recs.push(record(h, 1000));
  const cost = computeCost(recs, spec, t);
  assert.equal(cost.peakKwh, 0);
  assert.equal(cost.offpeakKwh, 10);
  assert.equal(cost.importedCost.toFixed(2), '1.00');
  assert.equal(cost.exportedCost, 0);
  assert.equal(cost.netCost.toFixed(2), '1.00');
});

test('computeCost: split peak / off-peak', () => {
  const t = normalizeTariff({ peakRate: 0.20, offpeakRate: 0.05, peakHours: [[9, 17]] });
  const recs = [7, 9, 12, 16, 18, 21].map((h) => record(h, 1000));
  const cost = computeCost(recs, spec, t);
  assert.equal(cost.peakKwh, 3);
  assert.equal(cost.offpeakKwh, 3);
  assert.equal(cost.peakCost.toFixed(2), '0.60');
  assert.equal(cost.offpeakCost.toFixed(2), '0.15');
  assert.equal(cost.importedCost.toFixed(2), '0.75');
});

test('computeCost: export is negative cost', () => {
  const t = normalizeTariff({ offpeakRate: 0.10 });
  const cost = computeCost([record(0, -500), record(1, 1000)], spec, t);
  assert.equal(cost.importedCost.toFixed(2), '0.10');
  assert.equal(cost.exportedCost.toFixed(2), '-0.05');
  assert.equal(cost.netCost.toFixed(2), '0.05');
});

test('parsePeakHoursString: comma list', () => {
  assert.deepEqual(parsePeakHoursString('09-17'), [[9, 17]]);
  assert.deepEqual(parsePeakHoursString('09:00-17:00'), [[9, 17]]);
  assert.deepEqual(parsePeakHoursString('09-12, 14-18'), [[9, 12], [14, 18]]);
  assert.deepEqual(parsePeakHoursString(''), []);
  assert.deepEqual(parsePeakHoursString('garbage'), []);
});

test('peakHoursToString: round-trip', () => {
  const original = '09:00-12:00, 14:00-18:00';
  const back = peakHoursToString(parsePeakHoursString(original));
  assert.equal(back, original);
});

test('normalizeTariff: coerces strings + missing fields', () => {
  const t = normalizeTariff({});
  assert.equal(t.currency, 'USD');
  assert.equal(t.peakRate, 0);
  assert.equal(t.offpeakRate, 0);
  assert.deepEqual(t.peakHours, []);
});
