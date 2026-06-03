// Feature H — timezone-aware formatting parity with python/.../tzutil.py.
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { test } from 'node:test';

import { formatLocalUtc, isoUtc, isoInZone, tzLabel } from '../tzutil.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..', '..');
const golden = JSON.parse(readFileSync(
  resolve(repoRoot, 'python', 'tests', 'fixtures', 'analysis_golden.json'), 'utf8'));

test('formatLocalUtc: UTC-only default matches Python golden', () => {
  const ms = golden.timezone.epoch_ms;
  assert.equal(formatLocalUtc(ms, null), golden.timezone.utc);
  assert.equal(formatLocalUtc(ms, 'UTC'), golden.timezone.utc);
});

test('formatLocalUtc: local + UTC matches Python golden (America/Chicago)', () => {
  const ms = golden.timezone.epoch_ms;
  assert.equal(formatLocalUtc(ms, 'America/Chicago'), golden.timezone.chicago);
});

test('isoUtc: emits +00:00 like Python isoformat', () => {
  assert.equal(isoUtc(Date.UTC(2024, 0, 13, 15, 0, 0)), '2024-01-13T15:00:00+00:00');
});

test('isoInZone: applies the zone offset', () => {
  // 15:00 UTC in summer DST for Chicago (CDT, -05:00).
  const summer = Date.UTC(2024, 6, 13, 15, 0, 0);
  assert.equal(isoInZone(summer, 'America/Chicago'), '2024-07-13T10:00:00-05:00');
});

test('tzLabel', () => {
  assert.equal(tzLabel(null), 'UTC');
  assert.equal(tzLabel('America/Chicago'), 'America/Chicago');
});
