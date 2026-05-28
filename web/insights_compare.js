// Cross-session insights — JS port of python/.../insights_compare.py.
// Both implementations share thresholds via spec/field_map.json's
// compare_insight_rules section.

function ruleVal(spec, key, fallback) {
  const v = spec?.compare_insight_rules?.[key];
  return v === undefined || v === null ? fallback : Number(v);
}

function fieldIndex(spec, name) {
  const f = spec.fields.find((f) => f.name === name);
  if (!f) throw new Error('spec missing ' + name);
  return f.index;
}

function mean(arr) {
  if (arr.length === 0) return 0;
  let s = 0;
  for (const v of arr) s += v;
  return s / arr.length;
}

function linreg(xs, ys) {
  const n = xs.length;
  if (n < 2) return { slope: 0, intercept: 0, r: 0 };
  const mx = mean(xs);
  const my = mean(ys);
  let sxy = 0, sxx = 0, syy = 0;
  for (let i = 0; i < n; i++) {
    const dx = xs[i] - mx;
    const dy = ys[i] - my;
    sxy += dx * dy; sxx += dx * dx; syy += dy * dy;
  }
  const slope = sxx ? sxy / sxx : 0;
  const intercept = my - slope * mx;
  const denom = Math.sqrt(sxx * syy);
  const r = denom ? sxy / denom : 0;
  return { slope, intercept, r };
}

function phaseAvg(records, spec, col) {
  const idx = fieldIndex(spec, col);
  const va = fieldIndex(spec, 'V_LN_a_avg_V');
  const vb = fieldIndex(spec, 'V_LN_b_avg_V');
  const vc = fieldIndex(spec, 'V_LN_c_avg_V');
  let sum = 0, n = 0;
  for (const r of records) {
    if (r.floats[va] > 50 && r.floats[vb] > 50 && r.floats[vc] > 50) {
      sum += r.floats[idx]; n++;
    }
  }
  return n ? sum / n : 0;
}

function pfLowFraction(records, spec, threshold = 0.85) {
  const pf = fieldIndex(spec, 'PF_total_avg');
  const va = fieldIndex(spec, 'V_LN_a_avg_V');
  const vb = fieldIndex(spec, 'V_LN_b_avg_V');
  const vc = fieldIndex(spec, 'V_LN_c_avg_V');
  let total = 0, hits = 0;
  for (const r of records) {
    if (r.floats[va] <= 50 || r.floats[vb] <= 50 || r.floats[vc] <= 50) continue;
    total++;
    const v = Math.abs(r.floats[pf]);
    if (v > 0 && v < threshold) hits++;
  }
  return total ? hits / total : 0;
}

function sessionStartMs(records) {
  return records.length ? records[0].startMs : null;
}

// --- Rules -----------------------------------------------------------------

function ruleVoltageDrift(sessions, spec) {
  if (sessions.length < 2) return [];
  const threshold = ruleVal(spec, 'voltage_drift_v_per_day', 0.1);
  const baseStart = sessionStartMs(sessions[0].records);
  if (baseStart === null) return [];
  const days = [];
  const labels = sessions.map((s) => s.label);
  const meansByPhase = { a: [], b: [], c: [] };
  for (const s of sessions) {
    const start = sessionStartMs(s.records);
    if (start === null) return [];
    days.push((start - baseStart) / 86_400_000);
    for (const ph of ['a', 'b', 'c']) {
      meansByPhase[ph].push(phaseAvg(s.records, spec, `V_LN_${ph}_avg_V`));
    }
  }
  if (Math.max(...days) - Math.min(...days) < 1e-6) return [];
  const out = [];
  for (const ph of ['a', 'b', 'c']) {
    const { slope } = linreg(days, meansByPhase[ph]);
    if (Math.abs(slope) < threshold) continue;
    const direction = slope > 0 ? 'rising' : 'falling';
    const sev = Math.abs(slope) >= 2 * threshold ? 'alert' : 'warn';
    out.push({
      id: -1, kind: 'voltage_drift', severity: sev,
      headline: `Phase ${ph.toUpperCase()} L-N voltage ${direction} ${Math.abs(slope).toFixed(2)} V/day across captures`,
      detail:
        `Linear fit of per-session phase ${ph.toUpperCase()} L-N average vs ` +
        `capture date shows a ${direction} trend of ${Math.abs(slope).toFixed(3)} V/day ` +
        `over ${sessions.length} session(s). Per-session means: ` +
        labels.map((lbl, i) => `${lbl}: ${meansByPhase[ph][i].toFixed(2)} V`).join(', ') + '.',
      sessionLabels: labels.slice(),
      recommendedActions: [
        "Verify transformer tap setting and feeder loading hasn't changed.",
        'If drift correlates with seasonal load or weather, may be normal.',
      ],
    });
  }
  return out;
}

function ruleRecurringOutages(sessions, spec) {
  if (sessions.length < 2) return [];
  const windowMins = Math.round(ruleVal(spec, 'recurring_outage_window_mins', 15));
  const byBucket = new Map();
  for (const s of sessions) {
    for (const ev of (s.events ?? [])) {
      if (ev.kind !== 'outage') continue;
      const d = new Date(ev.tStartMs);
      const todMins = d.getUTCHours() * 60 + d.getUTCMinutes();
      const bucket = Math.floor(todMins / windowMins) * windowMins;
      if (!byBucket.has(bucket)) byBucket.set(bucket, []);
      byBucket.get(bucket).push([s.label, d]);
    }
  }
  const out = [];
  for (const [bucket, hits] of byBucket.entries()) {
    const sessionsHit = new Set(hits.map(([lbl]) => lbl));
    if (sessionsHit.size < 2) continue;
    const hh = String(Math.floor(bucket / 60)).padStart(2, '0');
    const mm = String(bucket % 60).padStart(2, '0');
    out.push({
      id: -1, kind: 'recurring_outages',
      severity: sessionsHit.size < sessions.length ? 'warn' : 'alert',
      headline: `Recurring outages around ${hh}:${mm} in ${sessionsHit.size} of ${sessions.length} captures`,
      detail:
        `Outage events occurred within the same ${windowMins}-min window of the day ` +
        `across ${sessionsHit.size} different sessions. Pattern often points to a ` +
        'scheduled load, external switching, or upstream maintenance. Occurrences: ' +
        hits.map(([lbl, t]) => `${lbl} at ${t.toISOString()}`).join('; ') + '.',
      sessionLabels: [...sessionsHit].sort(),
      recommendedActions: [
        'Cross-reference with utility planned-work logs.',
        'Check on-site automation that might run on this schedule.',
      ],
    });
  }
  return out;
}

function rulePfDegradation(sessions, spec) {
  const minSessions = Math.round(ruleVal(spec, 'pf_degradation_min_sessions', 3));
  if (sessions.length < minSessions) return [];
  const minR = ruleVal(spec, 'pf_degradation_min_r', 0.5);
  const baseStart = sessionStartMs(sessions[0].records);
  if (baseStart === null) return [];
  const days = [], fracs = [], labels = [];
  for (const s of sessions) {
    const start = sessionStartMs(s.records);
    if (start === null) return [];
    days.push((start - baseStart) / 86_400_000);
    fracs.push(pfLowFraction(s.records, spec));
    labels.push(s.label);
  }
  if (Math.max(...days) - Math.min(...days) < 1e-6) return [];
  const { slope, r } = linreg(days, fracs);
  if (r < minR || slope <= 0) return [];
  return [{
    id: -1, kind: 'pf_degradation', severity: 'warn',
    headline: `Power factor degrading over time (slope ${slope.toFixed(4)}/day, r=${r.toFixed(2)})`,
    detail:
      `The fraction of operating time with |PF| < 0.85 is trending up across the ` +
      `${sessions.length} captured sessions (Pearson r=${r.toFixed(2)}). ` +
      'Per-session low-PF fractions: ' +
      labels.map((lbl, i) => `${lbl}: ${(fracs[i] * 100).toFixed(1)}%`).join(', ') + '.',
    sessionLabels: labels,
    recommendedActions: [
      'Investigate whether new inductive load has been added.',
      'Re-evaluate PFC sizing — the original headroom may have been consumed.',
    ],
  }];
}

function ruleStiffnessEmergence(sessions, _spec) {
  if (sessions.length < 2) return [];
  const enriched = sessions.map((s) => ({ s, start: sessionStartMs(s.records) }));
  if (enriched.some((x) => x.start === null)) return [];
  enriched.sort((a, b) => a.start - b.start);
  const sorted = enriched.map((x) => x.s);
  const hasStiff = sorted.map((s) =>
    (s.findings ?? []).some((f) => f.kind === 'freq_source_stiffness')
  );
  const half = Math.floor(hasStiff.length / 2);
  if (half === 0) return [];
  const earlierAny = hasStiff.slice(0, half).some(Boolean);
  const laterCount = hasStiff.slice(half).filter(Boolean).length;
  if (earlierAny || laterCount === 0) return [];
  const older = sorted.slice(0, half).map((s) => s.label);
  const newer = sorted.slice(half).map((s) => s.label);
  return [{
    id: -1, kind: 'source_stiffness_emergence', severity: 'warn',
    headline: `Weak-source signature emerged in ${laterCount} of ${hasStiff.length - half} recent captures`,
    detail:
      `Earlier captures (${older.join(', ')}) did not show the freq_source_stiffness ` +
      `finding, but recent captures (${newer.join(', ')}) do. The upstream supply ` +
      'impedance has likely grown — typical after a feeder reconfiguration or ' +
      'switchover to a generator/microgrid source.',
    sessionLabels: sorted.map((s) => s.label),
    recommendedActions: [
      "Confirm the supply topology hasn't changed.",
      'If on a microgrid or genset, check governor/droop settings.',
    ],
  }];
}

function ruleEventCountTrend(sessions, spec) {
  const minSessions = Math.round(ruleVal(spec, 'event_trend_min_sessions', 3));
  if (sessions.length < minSessions) return [];
  const minR = ruleVal(spec, 'event_trend_min_r', 0.7);
  const baseStart = sessionStartMs(sessions[0].records);
  if (baseStart === null) return [];
  const days = [];
  const labels = [];
  for (const s of sessions) {
    const start = sessionStartMs(s.records);
    if (start === null) return [];
    days.push((start - baseStart) / 86_400_000);
    labels.push(s.label);
  }
  if (Math.max(...days) - Math.min(...days) < 1e-6) return [];
  const kinds = new Set();
  for (const s of sessions) for (const ev of (s.events ?? [])) kinds.add(ev.kind);
  const out = [];
  for (const k of [...kinds].sort()) {
    const counts = sessions.map((s) =>
      (s.events ?? []).filter((ev) => ev.kind === k).length
    );
    if (counts.reduce((a, b) => a + b, 0) < 2) continue;
    const { slope, r } = linreg(days, counts);
    if (Math.abs(r) < minR || Math.abs(slope) < 0.01) continue;
    const direction = slope > 0 ? 'rising' : 'falling';
    out.push({
      id: -1, kind: `event_trend_${k}`,
      severity: slope > 0 ? 'warn' : 'info',
      headline: `${k} events ${direction} ${Math.abs(slope).toFixed(2)}/day (r=${r.toFixed(2)})`,
      detail:
        `Linear trend of ${k} event count across captures: ` +
        labels.map((lbl, i) => `${lbl}: ${counts[i]}`).join(', ') +
        `. Pearson r = ${r.toFixed(2)}, slope = ${slope.toFixed(3)} per day.`,
      sessionLabels: labels.slice(),
    });
  }
  return out;
}

// --- Public API ------------------------------------------------------------

const SEV_RANK = { alert: 0, warn: 1, info: 2 };

export function analyzeCompareInsights(sessions, spec) {
  const raw = [
    ...ruleVoltageDrift(sessions, spec),
    ...ruleRecurringOutages(sessions, spec),
    ...rulePfDegradation(sessions, spec),
    ...ruleStiffnessEmergence(sessions, spec),
    ...ruleEventCountTrend(sessions, spec),
  ];
  raw.sort((a, b) => (SEV_RANK[a.severity] - SEV_RANK[b.severity]) || a.kind.localeCompare(b.kind));
  return raw.map((f, i) => ({ ...f, id: i }));
}
