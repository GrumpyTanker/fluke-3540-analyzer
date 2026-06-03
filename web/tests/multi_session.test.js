// Unit tests for the MultiSession state container.
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { test } from 'node:test';

import { MultiSession, stitchStores } from '../multi_session.js';
import { ColumnStore } from '../column_store.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..', '..');
const spec = JSON.parse(readFileSync(resolve(repoRoot, 'spec', 'field_map.json'), 'utf8'));
const FI = new Map(spec.fields.map((f) => [f.name, f.index]));
const BASE = Date.UTC(2024, 0, 13, 22, 0, 0);

function mkStore(count, baseMs, pTotal = 50000) {
  const recs = [];
  for (let n = 0; n < count; n++) {
    const floats = new Float32Array(spec.data_floats);
    for (const ph of ['a', 'b', 'c']) {
      for (const st of ['min', 'max', 'avg']) {
        floats[FI.get(`V_LN_${ph}_${st}_V`)] = 277.0;
        floats[FI.get(`I_${ph}_${st}_A`)] = 100.0;
      }
    }
    floats[FI.get('freq_avg_Hz')] = 60.0;
    floats[FI.get('P_total_avg_W')] = pTotal;
    recs.push({ index: n, startMs: baseMs + n * 1000, endMs: baseMs + (n + 1) * 1000, floats });
  }
  return ColumnStore.fromRecords(recs, spec);
}

const mkSession = (label) => ({
  label, records: [], events: [], snapshots: [], findings: [],
  config: { asset_name: label }, fileHash: null,
});

test('MultiSession: starts empty', () => {
  const m = new MultiSession();
  assert.equal(m.count(), 0);
  assert.equal(m.getActive(), null);
  assert.equal(m.canCompare(), false);
});

test('MultiSession: add sets active to the new one', () => {
  const m = new MultiSession();
  m.add(mkSession('a'));
  assert.equal(m.count(), 1);
  assert.equal(m.getActive().label, 'a');
  m.add(mkSession('b'));
  assert.equal(m.getActive().label, 'b');
});

test('MultiSession: add dedups labels with suffix', () => {
  const m = new MultiSession();
  m.add(mkSession('foo'));
  m.add(mkSession('foo'));
  m.add(mkSession('foo'));
  const labels = m.getAll().map((s) => s.label);
  assert.deepEqual(labels, ['foo', 'foo-2', 'foo-3']);
});

test('MultiSession: assigns distinct colors', () => {
  const m = new MultiSession();
  for (let i = 0; i < 4; i++) m.add(mkSession('s' + i));
  const colors = m.getAll().map((s) => s.color);
  assert.equal(new Set(colors).size, 4);
});

test('MultiSession: remove + active falls back to last', () => {
  const m = new MultiSession();
  m.add(mkSession('a'));
  m.add(mkSession('b'));
  m.add(mkSession('c'));
  m.setActive('a');
  m.remove('a');
  // After removing active, fallback policy moves active to whatever index 'a' was at, or last if past end.
  assert.ok(m.getActive() !== null);
  m.remove('b');
  assert.equal(m.count(), 1);
  assert.equal(m.getActive().label, 'c');
  m.remove('c');
  assert.equal(m.getActive(), null);
});

test('MultiSession: rename', () => {
  const m = new MultiSession();
  m.add(mkSession('a'));
  m.add(mkSession('b'));
  assert.equal(m.rename('a', 'first'), true);
  assert.equal(m.getAll()[0].label, 'first');
  // can't rename to existing
  assert.equal(m.rename('first', 'b'), false);
  // can't rename to empty
  assert.equal(m.rename('first', ''), false);
  assert.equal(m.rename('first', '   '), false);
});

test('MultiSession: compareMode requires ≥2', () => {
  const m = new MultiSession();
  m.add(mkSession('a'));
  m.setCompareMode(true);
  assert.equal(m.compareMode, false);
  m.add(mkSession('b'));
  m.setCompareMode(true);
  assert.equal(m.compareMode, true);
  m.remove('b');
  assert.equal(m.compareMode, false);
});

test('MultiSession: listener fires on mutation', () => {
  const m = new MultiSession();
  let calls = 0;
  m.on(() => { calls++; });
  m.add(mkSession('a'));
  m.rename('a', 'b');
  m.setActive('b');
  m.remove('b');
  assert.ok(calls >= 4);
});

test('MultiSession: clear', () => {
  const m = new MultiSession();
  m.add(mkSession('a'));
  m.add(mkSession('b'));
  m.clear();
  assert.equal(m.count(), 0);
  assert.equal(m.getActive(), null);
  assert.equal(m.compareMode, false);
});

// --- Stitching (Feature D) — parity with python stitch.stitch_stores --------

test('stitchStores: consecutive sessions form one continuous store', () => {
  const s1 = mkStore(60, BASE, 11000);
  const s2 = mkStore(40, BASE + 60000, 22000);
  const res = stitchStores([{ label: 'S1', store: s1 }, { label: 'S2', store: s2 }]);
  assert.equal(res.store.n, 100);
  assert.equal(res.gaps.length, 0);
  // Monotonic, contiguous timeline.
  for (let i = 1; i < res.store.n; i++) {
    assert.ok(res.store.startMs[i] > res.store.startMs[i - 1]);
  }
  // Channel values carried through in order.
  const p = res.store.col('P_total_avg_W');
  assert.equal(p[0], 11000);
  assert.equal(p[60], 22000);
  // Provenance.
  assert.deepEqual(res.sources.map((s) => s.label), ['S1', 'S2']);
  assert.equal(res.sources[0].lo, 0);
  assert.equal(res.sources[0].hi, 60);
  assert.equal(res.sources[1].lo, 60);
  assert.equal(res.sources[1].hi, 100);
});

test('stitchStores: orders by start time regardless of input order', () => {
  const s1 = mkStore(10, BASE);
  const s2 = mkStore(10, BASE + 10000);
  const res = stitchStores([{ label: 'later', store: s2 }, { label: 'earlier', store: s1 }]);
  assert.deepEqual(res.sources.map((s) => s.label), ['earlier', 'later']);
});

test('stitchStores: records a gap when sessions do not abut', () => {
  const s1 = mkStore(60, BASE);
  const s2 = mkStore(60, BASE + 60000 + 3600000);  // 1h gap
  const res = stitchStores([{ label: 'S1', store: s1 }, { label: 'S2', store: s2 }]);
  assert.equal(res.store.n, 120);
  assert.equal(res.gaps.length, 1);
  assert.equal(res.gaps[0].after_label, 'S1');
  assert.equal(res.gaps[0].before_label, 'S2');
  assert.ok(Math.abs(res.gaps[0].seconds - 3600) < 1e-6);
});

test('stitchStores: small gap within tolerance is not recorded', () => {
  const s1 = mkStore(60, BASE);
  const s2 = mkStore(60, BASE + 61000);  // 1s gap, within 2s tol
  const res = stitchStores([{ label: 'S1', store: s1 }, { label: 'S2', store: s2 }]);
  assert.equal(res.gaps.length, 0);
});

test('MultiSession.buildStitched: stitches added store-backed sessions', () => {
  const m = new MultiSession();
  m.add({ label: 'A', store: mkStore(30, BASE), records: null, events: [], snapshots: [], findings: [], config: null });
  m.add({ label: 'B', store: mkStore(20, BASE + 30000), records: null, events: [], snapshots: [], findings: [], config: null });
  const res = m.buildStitched(spec);
  assert.equal(res.store.n, 50);
  assert.equal(res.gaps.length, 0);
  assert.deepEqual(res.sources.map((s) => s.label), ['A', 'B']);
});
