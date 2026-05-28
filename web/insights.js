// Insights engine — JavaScript port of python/.../insights.py.
//
// Both implementations share thresholds via spec/field_map.json's
// insight_rules section. Same Finding shape, same rule logic, same
// headline/detail templates so the web UI and CLI report look alike.

function ruleVal(spec, key, fallback) {
  const v = spec?.insight_rules?.[key];
  return v === undefined || v === null ? fallback : Number(v);
}

function fieldIndex(spec, name) {
  const f = spec.fields.find((f) => f.name === name);
  if (!f) throw new Error(`spec missing field ${name}`);
  return f.index;
}

function col(records, spec, name) {
  const idx = fieldIndex(spec, name);
  return records.map((r) => r.floats[idx]);
}

function nonOutageMask(records, spec) {
  const a = col(records, spec, 'V_LN_a_avg_V');
  const b = col(records, spec, 'V_LN_b_avg_V');
  const c = col(records, spec, 'V_LN_c_avg_V');
  return records.map((_, i) => a[i] > 50 && b[i] > 50 && c[i] > 50);
}

function mean(arr) {
  if (arr.length === 0) return 0;
  let s = 0;
  for (const v of arr) s += v;
  return s / arr.length;
}

// --- Individual rules -------------------------------------------------------

function ruleOutageSignatures(events, spec) {
  const windowMs = ruleVal(spec, 'outage_dip_window_secs', 30) * 1000;
  const findings = [];
  for (const ev of events) {
    if (ev.kind !== 'outage') continue;
    const related = [ev.id];
    const bits = [];
    const leading = events.find((e) =>
      e.kind === 'dip' && e.tEndMs >= ev.tStartMs - windowMs && e.tEndMs <= ev.tStartMs
    );
    if (leading) {
      related.push(leading.id);
      bits.push(
        `a leading dip on phase(s) ${(leading.affectedPhases ?? []).join('/') || '?'} ` +
        `(${(leading.severity * 277).toFixed(0)} V)`
      );
    }
    const trailingHi = events.find((e) =>
      e.kind === 'high_current' && e.tStartMs >= ev.tEndMs && e.tStartMs <= ev.tEndMs + windowMs
    );
    if (trailingHi) {
      related.push(trailingHi.id);
      bits.push(
        `restoration inrush on phase ${trailingHi.affectedPhases[0]} ` +
        `(${trailingHi.severity.toFixed(0)} A)`
      );
    }
    const trailingDip = events.find((e) =>
      e.kind === 'dip' && e.tStartMs >= ev.tEndMs && e.tStartMs <= ev.tEndMs + windowMs
    );
    if (trailingDip && !related.includes(trailingDip.id)) {
      related.push(trailingDip.id);
      bits.push(
        `a trailing dip on phase(s) ${(trailingDip.affectedPhases ?? []).join('/') || '?'}`
      );
    }
    const dur = Math.max(1, Math.round((ev.tEndMs - ev.tStartMs) / 1000));
    const hhmmss = new Date(ev.tStartMs).toISOString().slice(11, 19);
    const headline = `Outage at ${hhmmss} (${dur} s)`;
    const detail = bits.length
      ? `Outage at ${new Date(ev.tStartMs).toISOString()} lasted ${dur} s, with ${bits.join(', ')}.`
      : `Outage at ${new Date(ev.tStartMs).toISOString()} lasted ${dur} s. ` +
        `No leading dip or restoration inrush was detected in the ` +
        `${windowMs / 1000}-s windows around the edges, which is consistent ` +
        'with a hard supply loss.';
    findings.push({
      id: -1, kind: 'outage_signature',
      severity: dur >= 60 ? 'alert' : 'warn',
      headline, detail,
      relatedEventIds: related,
      recommendedActions: [
        "Correlate with the utility's outage records or with on-site switchgear logs.",
        'If inrush was observed, verify that downstream breakers and transformer ' +
        'protection hold up under the recorded current.',
      ],
    });
  }
  return findings;
}

function rulePhaseAsymmetry(records, spec) {
  const threshold = ruleVal(spec, 'phase_asymmetry_pct', 2.0);
  const notOut = nonOutageMask(records, spec);
  const a = col(records, spec, 'V_LN_a_avg_V');
  const b = col(records, spec, 'V_LN_b_avg_V');
  const c = col(records, spec, 'V_LN_c_avg_V');
  const aVals = a.filter((_, i) => notOut[i]);
  const bVals = b.filter((_, i) => notOut[i]);
  const cVals = c.filter((_, i) => notOut[i]);
  if (aVals.length === 0) return [];
  const means = { a: mean(aVals), b: mean(bVals), c: mean(cVals) };
  const overall = (means.a + means.b + means.c) / 3;
  const spread = Math.max(means.a, means.b, means.c) - Math.min(means.a, means.b, means.c);
  const spreadPct = spread / overall * 100;
  if (spreadPct < threshold) return [];
  const hottest = Object.entries(means).reduce((m, x) => x[1] > m[1] ? x : m)[0];
  const coolest = Object.entries(means).reduce((m, x) => x[1] < m[1] ? x : m)[0];
  return [{
    id: -1, kind: 'phase_asymmetry',
    severity: spreadPct >= 3.0 ? 'alert' : 'warn',
    headline: `Phase asymmetry ${spreadPct.toFixed(1)}% across the session (phase ${hottest.toUpperCase()} hottest)`,
    detail:
      `Per-phase L-N voltage average across non-outage samples: ` +
      `A=${means.a.toFixed(1)} V, B=${means.b.toFixed(1)} V, C=${means.c.toFixed(1)} V. ` +
      `That is ${spreadPct.toFixed(2)}% spread relative to the mean ` +
      `(${overall.toFixed(1)} V). Phase ${hottest.toUpperCase()} runs hottest; ` +
      `phase ${coolest.toUpperCase()} runs coldest.`,
    relatedEventIds: [],
    recommendedActions: [
      'Check transformer tap setting.',
      'Review per-phase load balance (large single-phase loads concentrated on ' +
      'one phase often produce this).',
      'If asymmetry persists across multiple captures, schedule a thermography scan.',
    ],
  }];
}

function rulePfDrift(records, spec) {
  const threshold = ruleVal(spec, 'pf_drift_threshold', 0.85);
  const minFrac = ruleVal(spec, 'pf_drift_min_fraction', 0.10);
  const notOut = nonOutageMask(records, spec);
  const pf = col(records, spec, 'PF_total_avg');
  const q = col(records, spec, 'Q_total_avg_VAR');
  const s = col(records, spec, 'S_total_avg_VA');
  const lowMask = records.map((_, i) =>
    notOut[i] && Math.abs(pf[i]) > 0 && Math.abs(pf[i]) < threshold
  );
  const total = lowMask.length;
  const hits = lowMask.filter(Boolean).length;
  const frac = total ? hits / total : 0;
  if (frac < minFrac) return [];
  const qDuring = q.filter((_, i) => lowMask[i]).map(Math.abs);
  const sDuring = s.filter((_, i) => lowMask[i]).map(Math.abs);
  const recKvar = Math.round(mean(qDuring) / 1000);
  const avgSKva = mean(sDuring) / 1000;
  return [{
    id: -1, kind: 'pf_drift',
    severity: frac < 0.30 ? 'warn' : 'alert',
    headline: `Power factor below ${threshold.toFixed(2)} for ${(frac * 100).toFixed(1)}% of non-outage time`,
    detail:
      `True PF dipped under ${threshold.toFixed(2)} on the total channel for ` +
      `${(frac * 100).toFixed(1)}% of operating time. Average apparent power during ` +
      `those periods was ${avgSKva.toFixed(1)} kVA with about ` +
      `${recKvar} kVAR of reactive demand. A correctly-sized power-factor ` +
      'correction bank would recover most of that as reduced current draw.',
    relatedEventIds: [],
    recommendedActions: [
      `Consider a fixed or stepped PFC bank in the ${Math.max(recKvar, 5)} kVAR class ` +
      '(verify with full kVAR distribution before committing).',
      'Re-measure after correction to confirm PF rises above the threshold.',
      'If the load varies widely, a stepped bank or active PFC unit may be needed.',
    ],
  }];
}

function ruleImbalanceSustained(records, spec) {
  const pctThreshold = ruleVal(spec, 'imbalance_sustained_pct', 1.5);
  const secsThreshold = Math.round(ruleVal(spec, 'imbalance_sustained_secs', 60));
  const notOut = nonOutageMask(records, spec);
  const a = col(records, spec, 'V_LN_a_avg_V');
  const b = col(records, spec, 'V_LN_b_avg_V');
  const c = col(records, spec, 'V_LN_c_avg_V');
  const imbal = new Array(records.length).fill(0);
  for (let i = 0; i < records.length; i++) {
    if (!notOut[i]) continue;
    const m = (a[i] + b[i] + c[i]) / 3;
    if (m <= 50) continue;
    imbal[i] = (Math.max(a[i], b[i], c[i]) - Math.min(a[i], b[i], c[i])) / m * 100;
  }
  const runs = [];
  let inRun = false, start = 0;
  for (let i = 0; i < imbal.length; i++) {
    if (imbal[i] > pctThreshold && notOut[i]) {
      if (!inRun) { inRun = true; start = i; }
    } else if (inRun) {
      if (i - start >= secsThreshold) runs.push([start, i - 1]);
      inRun = false;
    }
  }
  if (inRun && imbal.length - start >= secsThreshold) runs.push([start, imbal.length - 1]);
  if (runs.length === 0) return [];
  const totalSecs = runs.reduce((s, [a_, b_]) => s + (b_ - a_ + 1), 0);
  let peakPct = 0;
  for (const [a_, b_] of runs) {
    for (let i = a_; i <= b_; i++) if (imbal[i] > peakPct) peakPct = imbal[i];
  }
  return [{
    id: -1, kind: 'imbalance_sustained', severity: 'warn',
    headline: `Imbalance > ${pctThreshold.toFixed(1)}% sustained for ${totalSecs} s total (${runs.length} window(s))`,
    detail:
      `NEMA voltage imbalance exceeded ${pctThreshold.toFixed(1)}% for ` +
      `${totalSecs} cumulative seconds across ${runs.length} window(s) of at ` +
      `least ${secsThreshold} s. Peak imbalance during those windows was ` +
      `${peakPct.toFixed(2)}%.`,
    relatedEventIds: [],
    recommendedActions: [
      'Identify the single-phase loads driving the imbalance and rebalance ' +
      'across phases if possible.',
      'Sustained imbalance derates motors by approximately (2 × imbalance_pct)^2 — ' +
      'verify motor nameplates and thermal margin.',
    ],
  }];
}

function ruleFreqStiffness(records, events, spec) {
  const thresholdHz = ruleVal(spec, 'freq_stiffness_hz', 0.05);
  const minCount = Math.round(ruleVal(spec, 'freq_stiffness_min_count', 3));
  const freq = col(records, spec, 'freq_avg_Hz');
  const indexByTime = new Map(records.map((r, i) => [r.startMs, i]));
  const correlations = [];
  for (const ev of events) {
    if (ev.kind !== 'power_step') continue;
    const i = indexByTime.get(ev.tStartMs);
    if (i === undefined) continue;
    const winStart = Math.max(0, i - 2);
    const winEnd = Math.min(freq.length - 1, i + 2);
    let maxDev = 0;
    for (let k = winStart; k <= winEnd; k++) {
      maxDev = Math.max(maxDev, Math.abs(freq[k] - 60));
    }
    if (maxDev > thresholdHz) correlations.push(ev);
  }
  if (correlations.length < minCount) return [];
  const stepCount = events.filter((e) => e.kind === 'power_step').length;
  return [{
    id: -1, kind: 'freq_source_stiffness', severity: 'warn',
    headline: `${correlations.length} power steps correlated with freq deviation > ${thresholdHz.toFixed(2)} Hz`,
    detail:
      `Of the ${stepCount} detected power_step events, ${correlations.length} ` +
      `coincided with a line-frequency deviation greater than ${thresholdHz.toFixed(2)} Hz ` +
      'inside a ±2-second window. This is consistent with a weak source: the upstream ' +
      'impedance is high enough that load changes pull the frequency around.',
    relatedEventIds: correlations.map((e) => e.id),
    recommendedActions: [
      'If the site is on a generator or microgrid, review droop / governor settings.',
      'On a grid-connected site, this often points to a long radial feeder.',
    ],
  }];
}

function ruleOutageFrequency(records, events, spec) {
  const perDayThreshold = ruleVal(spec, 'outage_frequency_per_day', 1.0);
  const outages = events.filter((e) => e.kind === 'outage');
  if (outages.length === 0 || records.length === 0) return [];
  const durSecs = (records[records.length - 1].endMs - records[0].startMs) / 1000;
  if (durSecs <= 0) return [];
  const days = durSecs / 86400;
  const rate = outages.length / days;
  if (rate < perDayThreshold) return [];
  return [{
    id: -1, kind: 'outage_frequency',
    severity: rate >= 2 * perDayThreshold ? 'alert' : 'warn',
    headline: `${outages.length} outage(s) in ${days.toFixed(2)} day(s) (${rate.toFixed(2)}/day)`,
    detail:
      `The session captured ${outages.length} outage events over ${days.toFixed(2)} days, ` +
      `a rate of ${rate.toFixed(2)} per day. Most utility benchmarks treat anything ` +
      'above one customer outage per year as poor reliability for industrial feeders.',
    relatedEventIds: outages.map((e) => e.id),
    recommendedActions: [
      'Open a service ticket with the utility citing the timestamps.',
      'If outages are sustained, consider a UPS for control loops.',
    ],
  }];
}

function ruleCurrentSpikeRatio(records, spec) {
  const threshold = ruleVal(spec, 'current_to_mean_ratio_alert', 5.0);
  const findings = [];
  const notOut = nonOutageMask(records, spec);
  for (const phase of ['a', 'b', 'c']) {
    const iMax = col(records, spec, `I_${phase}_max_A`);
    const valid = iMax.filter((_, i) => notOut[i]);
    if (valid.length === 0) continue;
    const m = mean(valid);
    const peak = Math.max(...valid);
    if (m <= 0) continue;
    const ratio = peak / m;
    if (ratio < threshold) continue;
    findings.push({
      id: -1, kind: 'current_spike_ratio', severity: 'info',
      headline: `Phase ${phase.toUpperCase()} peak current ${peak.toFixed(0)} A is ${ratio.toFixed(1)}× the steady mean (${m.toFixed(0)} A)`,
      detail:
        `Phase ${phase.toUpperCase()} sustained an average current of ${m.toFixed(1)} A ` +
        `during non-outage operation but reached a peak of ${peak.toFixed(1)} A. ` +
        `A ${ratio.toFixed(1)}× peak-to-mean ratio is typical for motor starts or ` +
        "restoration inrush — verify it stays within the upstream breaker's " +
        'instantaneous trip curve.',
      relatedEventIds: [],
      recommendedActions: [
        "Compare the peak against the breaker's instantaneous trip multiple.",
        'If this is a recurring motor inrush, soft-starter or VFD retrofits ' +
        'can cut peak current to ~2-3× nominal.',
      ],
    });
  }
  return findings;
}

// --- Public API -------------------------------------------------------------

const SEV_RANK = { alert: 0, warn: 1, info: 2 };

export function analyzeInsights(records, events, spec, _snapshots = [], _config = null) {
  const raw = [
    ...ruleOutageSignatures(events, spec),
    ...rulePhaseAsymmetry(records, spec),
    ...rulePfDrift(records, spec),
    ...ruleImbalanceSustained(records, spec),
    ...ruleFreqStiffness(records, events, spec),
    ...ruleOutageFrequency(records, events, spec),
    ...ruleCurrentSpikeRatio(records, spec),
  ];
  raw.sort((a, b) => (SEV_RANK[a.severity] - SEV_RANK[b.severity]) || a.kind.localeCompare(b.kind));
  return raw.map((f, i) => ({ ...f, id: i }));
}
