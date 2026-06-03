// Tests for min/max chart decimation (large-session web hardening).
import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { decimateSeries, DECIMATE_THRESHOLD } from '../plots.js';

function makeData(n, fn) {
  const xs = [];
  const ys = [];
  for (let i = 0; i < n; i++) {
    xs.push(i);
    ys.push(fn(i));
  }
  return [xs, ys];
}

test('returns data unchanged when under target', () => {
  const data = makeData(100, (i) => i);
  const res = decimateSeries(data, 4000);
  assert.equal(res.decimated, false);
  assert.equal(res.data, data);
  assert.equal(res.originalPoints, 100);
});

test('decimates when over target', () => {
  const data = makeData(100000, (i) => Math.sin(i / 100));
  const res = decimateSeries(data, 4000);
  assert.equal(res.decimated, true);
  assert.equal(res.originalPoints, 100000);
  // ~target points (2 per bucket * ~2000 buckets)
  assert.ok(res.data[0].length <= 4000 + 4);
  assert.ok(res.data[0].length >= 1000);
});

test('preserves the global min and max spikes', () => {
  // Flat 100 with a single deep dip and a single tall spike.
  const n = 50000;
  const data = makeData(n, (i) => {
    if (i === 12345) return -999; // dip
    if (i === 38000) return 999;  // spike
    return 100;
  });
  const res = decimateSeries(data, 2000);
  assert.equal(res.decimated, true);
  const ys = res.data[1];
  assert.ok(ys.includes(-999), 'dip survived decimation');
  assert.ok(ys.includes(999), 'spike survived decimation');
});

test('x stays monotonic non-decreasing', () => {
  const n = 60000;
  const data = makeData(n, (i) => (i % 1000) - 500);
  const res = decimateSeries(data, 3000);
  const xs = res.data[0];
  for (let i = 1; i < xs.length; i++) {
    assert.ok(xs[i] >= xs[i - 1], `x not monotonic at ${i}`);
  }
});

test('multi-series decimation keeps all series aligned', () => {
  const n = 40000;
  const xs = [];
  const y0 = [];
  const y1 = [];
  for (let i = 0; i < n; i++) {
    xs.push(i);
    y0.push(Math.sin(i / 50));
    y1.push(Math.cos(i / 50));
  }
  const res = decimateSeries([xs, y0, y1], 2000);
  assert.equal(res.data.length, 3);
  assert.equal(res.data[1].length, res.data[0].length);
  assert.equal(res.data[2].length, res.data[0].length);
});

test('threshold constant is exported and sane', () => {
  assert.ok(DECIMATE_THRESHOLD > 1000);
});
