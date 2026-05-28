// TOU tariff calculator — JS port of python/.../tariff.py.

function fieldIndex(spec, name) {
  const f = spec.fields.find((x) => x.name === name);
  return f ? f.index : -1;
}

/**
 * @typedef {object} Tariff
 * @property {string} currency
 * @property {number} peakRate     - $/kWh
 * @property {number} offpeakRate  - $/kWh
 * @property {Array<[number, number]>} peakHours - [[startH, endH], ...] in UTC
 */

export function isPeak(tariff, hour) {
  for (const [start, end] of tariff.peakHours ?? []) {
    if (start === end) continue;
    if (start < end) {
      if (start <= hour && hour < end) return true;
    } else {
      if (hour >= start || hour < end) return true;
    }
  }
  return false;
}

export function emptyCost(currency = 'USD') {
  return {
    currency,
    peakKwh: 0, offpeakKwh: 0,
    peakImportedKwh: 0, peakExportedKwh: 0,
    offpeakImportedKwh: 0, offpeakExportedKwh: 0,
    peakCost: 0, offpeakCost: 0,
    importedCost: 0, exportedCost: 0, netCost: 0,
  };
}

export function computeCost(records, spec, tariff) {
  if (!tariff) return emptyCost();
  const whIdx = fieldIndex(spec, 'Wh_total');
  if (whIdx < 0) return emptyCost(tariff.currency);
  let pkImp = 0, pkExp = 0, opImp = 0, opExp = 0;
  for (const r of records) {
    const wh = r.floats[whIdx];
    if (!wh) continue;
    const hour = new Date(r.startMs).getUTCHours();
    const peak = isPeak(tariff, hour);
    if (wh > 0) {
      if (peak) pkImp += wh; else opImp += wh;
    } else {
      if (peak) pkExp += wh; else opExp += wh;
    }
  }
  const pkImpK = pkImp / 1000, pkExpK = pkExp / 1000;
  const opImpK = opImp / 1000, opExpK = opExp / 1000;
  const peakImpCost = pkImpK * tariff.peakRate;
  const peakExpCost = pkExpK * tariff.peakRate;
  const offImpCost = opImpK * tariff.offpeakRate;
  const offExpCost = opExpK * tariff.offpeakRate;
  return {
    currency: tariff.currency,
    peakKwh: pkImpK + pkExpK,
    offpeakKwh: opImpK + opExpK,
    peakImportedKwh: pkImpK,
    peakExportedKwh: pkExpK,
    offpeakImportedKwh: opImpK,
    offpeakExportedKwh: opExpK,
    peakCost: peakImpCost + peakExpCost,
    offpeakCost: offImpCost + offExpCost,
    importedCost: peakImpCost + offImpCost,
    exportedCost: peakExpCost + offExpCost,
    netCost: (peakImpCost + offImpCost) + (peakExpCost + offExpCost),
  };
}

// --- Tariff serialisation for localStorage ----------------------------------

const TARIFF_PREFIX = 'tariff:';

export function tariffStorageKey(assetName) {
  return TARIFF_PREFIX + (assetName || 'default').replace(/[^a-zA-Z0-9._-]+/g, '_');
}

export function loadTariff(assetName) {
  try {
    const raw = localStorage.getItem(tariffStorageKey(assetName));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return normalizeTariff(parsed);
  } catch (_) {
    return null;
  }
}

export function saveTariff(assetName, tariff) {
  try {
    localStorage.setItem(tariffStorageKey(assetName), JSON.stringify(tariff));
  } catch (_) {/* quota etc. */}
}

export function normalizeTariff(t) {
  return {
    currency: String(t?.currency ?? 'USD'),
    peakRate: Number(t?.peakRate ?? 0) || 0,
    offpeakRate: Number(t?.offpeakRate ?? 0) || 0,
    peakHours: Array.isArray(t?.peakHours)
      ? t.peakHours.map(([a, b]) => [Number(a) | 0, Number(b) | 0])
      : [],
  };
}

/**
 * Parse "09:00-21:00, 14:00-15:00" (HH:MM hour ranges) into peakHours.
 * Minutes are dropped for v0.4 (hour-of-day buckets).
 */
export function parsePeakHoursString(text) {
  if (!text || !text.trim()) return [];
  const out = [];
  for (const piece of text.split(',')) {
    const m = piece.trim().match(/^(\d{1,2})(?::\d{2})?\s*-\s*(\d{1,2})(?::\d{2})?$/);
    if (!m) continue;
    const a = parseInt(m[1], 10);
    const b = parseInt(m[2], 10);
    if (a >= 0 && a < 24 && b >= 0 && b < 24) out.push([a, b]);
  }
  return out;
}

export function peakHoursToString(peakHours) {
  return (peakHours ?? [])
    .map(([a, b]) => `${String(a).padStart(2, '0')}:00-${String(b).padStart(2, '0')}:00`)
    .join(', ');
}
