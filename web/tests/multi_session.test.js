// Unit tests for the MultiSession state container.
import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { MultiSession } from '../multi_session.js';

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
