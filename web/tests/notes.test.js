// Tests for the per-event notes helper.
import { strict as assert } from 'node:assert';
import { test, before } from 'node:test';

import {
  clearAllNotes, exportNotesJson, getAllNotes, getNote, noteKey, setNote,
} from '../notes.js';

// Minimal localStorage polyfill for Node tests.
class MemStorage {
  constructor() { this.map = new Map(); }
  get length() { return this.map.size; }
  key(i) { return [...this.map.keys()][i] ?? null; }
  getItem(k) { return this.map.has(k) ? this.map.get(k) : null; }
  setItem(k, v) { this.map.set(k, String(v)); }
  removeItem(k) { this.map.delete(k); }
}

before(() => { globalThis.localStorage = new MemStorage(); });

test('noteKey: namespaced by file hash + event id', () => {
  assert.equal(noteKey('abc', 5), 'note:abc:5');
  assert.equal(noteKey(null, 5), 'note:unknown:5');
});

test('setNote / getNote round-trip', () => {
  setNote('h1', 1, 'hello world');
  assert.equal(getNote('h1', 1), 'hello world');
});

test('setNote with empty / whitespace removes the entry', () => {
  setNote('h2', 2, 'something');
  setNote('h2', 2, '   ');
  assert.equal(getNote('h2', 2), '');
});

test('getAllNotes scoped to a file hash', () => {
  setNote('hX', 1, 'A');
  setNote('hX', 7, 'B');
  setNote('hY', 1, 'other');
  const all = getAllNotes('hX');
  assert.deepEqual(all, { 1: 'A', 7: 'B' });
});

test('clearAllNotes removes everything for that hash', () => {
  setNote('hZ', 1, 'A');
  setNote('hZ', 2, 'B');
  setNote('hW', 1, 'survivor');
  clearAllNotes('hZ');
  assert.deepEqual(getAllNotes('hZ'), {});
  assert.equal(getNote('hW', 1), 'survivor');
});

test('exportNotesJson emits a self-describing object', () => {
  setNote('hExport', 3, 'Look at this');
  const json = exportNotesJson('hExport', [
    { id: 3, kind: 'dip', tStartMs: Date.UTC(2024, 0, 13, 22, 0, 0) },
    { id: 4, kind: 'outage', tStartMs: Date.UTC(2024, 0, 13, 22, 5, 0) },
  ]);
  const parsed = JSON.parse(json);
  assert.equal(parsed.fileHash, 'hExport');
  assert.equal(parsed.notes.length, 1);
  assert.equal(parsed.notes[0].id, 3);
  assert.equal(parsed.notes[0].kind, 'dip');
  assert.equal(parsed.notes[0].note, 'Look at this');
});
