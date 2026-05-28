// Unit tests for range-select pure helpers (URL hash codec + record scoping).
// The renderRangeSelector() DOM wiring is tested manually in a browser.
import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import {
  rangeFromHash, rangeToHash, scopeRecordsToRange,
} from '../range_select.js';


test('rangeToHash: encodes start/end as ISO', () => {
  const h = rangeToHash({
    startMs: Date.UTC(2024, 0, 13, 22, 50, 0),
    endMs:   Date.UTC(2024, 0, 13, 22, 55, 0),
  });
  assert.equal(h, '#t=2024-01-13T22:50:00.000Z/2024-01-13T22:55:00.000Z');
});

test('rangeToHash: null → empty', () => {
  assert.equal(rangeToHash(null), '');
});

test('rangeFromHash: round-trips with rangeToHash', () => {
  const orig = {
    startMs: Date.UTC(2024, 5, 1, 12, 0, 0),
    endMs:   Date.UTC(2024, 5, 1, 13, 30, 0),
  };
  const parsed = rangeFromHash(rangeToHash(orig));
  assert.deepEqual(parsed, orig);
});

test('rangeFromHash: rejects empty / malformed input', () => {
  assert.equal(rangeFromHash(''), null);
  assert.equal(rangeFromHash('#foo=bar'), null);
  assert.equal(rangeFromHash('#t=garbage'), null);
  assert.equal(rangeFromHash('#t=2024-01-13/notadate'), null);
});

test('rangeFromHash: rejects end <= start', () => {
  assert.equal(
    rangeFromHash('#t=2024-01-13T22:55:00.000Z/2024-01-13T22:50:00.000Z'),
    null,
  );
});

test('scopeRecordsToRange: null range returns all', () => {
  const recs = Array.from({ length: 5 }, (_, i) => ({ startMs: i * 1000 }));
  assert.equal(scopeRecordsToRange(recs, null), recs);
});

test('scopeRecordsToRange: filters by startMs', () => {
  const recs = Array.from({ length: 10 }, (_, i) => ({ startMs: i * 1000 }));
  const out = scopeRecordsToRange(recs, { startMs: 3000, endMs: 7000 });
  assert.deepEqual(out.map((r) => r.startMs), [3000, 4000, 5000, 6000, 7000]);
});

test('scopeRecordsToRange: empty result when range outside data', () => {
  const recs = Array.from({ length: 5 }, (_, i) => ({ startMs: i * 1000 }));
  const out = scopeRecordsToRange(recs, { startMs: 100_000, endMs: 200_000 });
  assert.equal(out.length, 0);
});
