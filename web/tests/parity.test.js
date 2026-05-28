// Parity test — parse the same synthetic_trend.bin fixture as the Python
// tests and verify the JS parser produces identical results.
//
// Run with:  cd web && npm test
// or:        node --test web/tests/parity.test.js
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { test } from 'node:test';

import {
  FILETIME_EPOCH_DIFF_MS,
  buildIndex,
  filetimeToUnixMs,
  parseTrendBin,
} from '../parser.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..', '..');
const SPEC_PATH = resolve(repoRoot, 'spec', 'field_map.json');
const FIXTURE_PATH = resolve(
  repoRoot, 'python', 'tests', 'fixtures', 'synthetic_trend.bin'
);

const spec = JSON.parse(readFileSync(SPEC_PATH, 'utf8'));

function loadFixture() {
  const buf = readFileSync(FIXTURE_PATH);
  // ArrayBuffer view aligned to the underlying allocation
  return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
}

// Must match python/tests/conftest.py exactly.
const SYNTHETIC_RECORD_COUNT = 10;
const SYNTHETIC_BASE_UTC_MS = Date.UTC(2024, 0, 13, 22, 0, 0);
function syntheticValue(recordIndex, fieldIndex) {
  return Math.fround((recordIndex + 1) * 100 + fieldIndex * 0.5);
}

test('spec sanity matches Python expectations', () => {
  const idx = buildIndex(spec);
  assert.equal(idx.recordSize, 744);
  assert.equal(idx.headerBytes, 24);
  assert.equal(idx.dataFloats, 180);
  assert.deepEqual([...idx.recordMagic], [70, 0, 232, 2]);
  assert.ok(idx.reverseCtsIndices.size > 0, 'expected reverseCtsIndices to be populated');
});

test('FILETIME epoch constant produces a unix-epoch round-trip', () => {
  // FILETIME 0 = 1601-01-01 = -11644473600000 ms vs unix epoch
  assert.equal(filetimeToUnixMs(0n), -Number(FILETIME_EPOCH_DIFF_MS));
});

test('parseTrendBin: record count matches Python', () => {
  const { records } = parseTrendBin(loadFixture(), spec);
  assert.equal(records.length, SYNTHETIC_RECORD_COUNT);
});

test('parseTrendBin: timestamps match Python record spacing', () => {
  const { records } = parseTrendBin(loadFixture(), spec);
  for (let n = 0; n < SYNTHETIC_RECORD_COUNT; n++) {
    const expectedStart = SYNTHETIC_BASE_UTC_MS + n * 1000;
    const expectedEnd = SYNTHETIC_BASE_UTC_MS + (n + 1) * 1000;
    assert.equal(records[n].startMs, expectedStart,
      `record ${n} start mismatch`);
    assert.equal(records[n].endMs, expectedEnd,
      `record ${n} end mismatch`);
  }
});

test('parseTrendBin: float values match the deterministic formula', () => {
  const { records } = parseTrendBin(loadFixture(), spec);
  // Sample a representative grid of cells (same cells as Python test)
  const checks = [
    [0, 0], [0, 47], [5, 100], [9, 179],
    [3, 50], [7, 144], [9, 0],
  ];
  for (const [n, i] of checks) {
    const expected = syntheticValue(n, i);
    const actual = records[n].floats[i];
    assert.equal(actual, expected,
      `record[${n}].floats[${i}] = ${actual}, expected ${expected}`);
  }
});

test('parseTrendBin: every cell matches Python (full table check)', () => {
  // Exhaustive — 10 * 180 = 1800 cells. Cheap, catches off-by-one alignment bugs.
  const { records } = parseTrendBin(loadFixture(), spec);
  let mismatchCount = 0;
  const firstFew = [];
  for (let n = 0; n < SYNTHETIC_RECORD_COUNT; n++) {
    for (let i = 0; i < 180; i++) {
      const expected = syntheticValue(n, i);
      const actual = records[n].floats[i];
      if (actual !== expected) {
        mismatchCount++;
        if (firstFew.length < 5) {
          firstFew.push(`  record[${n}].floats[${i}] = ${actual} vs expected ${expected}`);
        }
      }
    }
  }
  assert.equal(mismatchCount, 0,
    `${mismatchCount} mismatches. First few:\n${firstFew.join('\n')}`);
});

test('parseTrendBin: reverseCts negates correct columns', () => {
  const buf = loadFixture();
  const plain = parseTrendBin(buf, spec, { reverseCts: false });
  const flipped = parseTrendBin(buf, spec, { reverseCts: true });
  const idx = buildIndex(spec);
  // Sample reverseCts indices and non-reverseCts indices
  const byName = new Map(idx.fields.map((f) => [f.name, f.index]));
  const flipNames = ['P_a_avg_W', 'Q_a_avg_VAR', 'PF_total_avg',
                     'DPF_a_avg', 'Wh_a', 'VARh_total'];
  const keepNames = ['V_LN_a_avg_V', 'V_LL_ab_avg_V', 'I_a_avg_A',
                     'S_a_avg_VA', 'freq_avg_Hz', 'VAh_a'];
  for (const name of flipNames) {
    const ci = byName.get(name);
    assert.equal(
      flipped.records[0].floats[ci], -plain.records[0].floats[ci],
      `${name} should be negated by reverseCts`
    );
  }
  for (const name of keepNames) {
    const ci = byName.get(name);
    assert.equal(
      flipped.records[0].floats[ci], plain.records[0].floats[ci],
      `${name} should NOT be negated by reverseCts`
    );
  }
});

test('parseTrendBin: throws on bad magic', () => {
  const corrupted = new Uint8Array(744).buffer; // all zeros, no magic
  assert.throws(() => parseTrendBin(corrupted, spec), /Bad magic/);
});
