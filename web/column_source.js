// Small adapter so the analysis engines (events / snapshots / insights / stats)
// can read columns from EITHER an array of record objects (legacy / tests) OR a
// ColumnStore (the memory-bounded streaming path). Both expose the same handful
// of accessors the engines need: column(name), startMs(i), endMs(i), length.

import { ColumnStore } from './column_store.js';

/**
 * Wrap a records-array or a ColumnStore into a uniform column source.
 * @param {Array|ColumnStore} source
 * @param {object} spec parsed field_map.json
 * @returns {{length:number, startMs:(i:number)=>number, endMs:(i:number)=>number,
 *            column:(name:string)=>(Float32Array|number[]), isStore:boolean}}
 */
export function asColumnSource(source, spec) {
  if (source instanceof ColumnStore) {
    return {
      length: source.n,
      isStore: true,
      startMs: (i) => source.startMs[i],
      endMs: (i) => source.endMs[i],
      column: (name) => source.col(name),
    };
  }
  // records array
  const records = source;
  const fi = new Map(spec.fields.map((f) => [f.name, f.index]));
  return {
    length: records.length,
    isStore: false,
    startMs: (i) => records[i].startMs,
    endMs: (i) => records[i].endMs,
    column: (name) => {
      const idx = fi.get(name);
      if (idx === undefined) throw new Error(`spec is missing field ${name}`);
      return Float32Array.from(records, (r) => r.floats[idx]);
    },
  };
}
