// Auto-narrative / executive summary (Feature E) — JS port of
// python/.../narrative.py. Deterministic, rule-based (no LLM); produces the
// same prose as the CLI so web and CLI reports read alike.

function fmtDuration(secs) {
  secs = Math.round(secs);
  if (secs >= 3600) return `${(secs / 3600).toFixed(1)} h`;
  if (secs >= 60) return `${(secs / 60).toFixed(1)} min`;
  return `${secs} s`;
}

function hhmm(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
    `${p(d.getUTCHours())}:${p(d.getUTCMinutes())} UTC`;
}

/**
 * Build a deterministic plain-English executive summary.
 * @param {Array} events  detected events ({kind,tStartMs,tEndMs,severity,affectedPhases})
 * @param {Array} findings insights ({kind,severity,headline})
 * @param {object|null} stats whole_session_stats dict
 * @param {object|null} ctReversal detectCtReversal output
 * @param {object} [opts] {config, totalRecords, durationSecs}
 * @returns {string}
 */
export function buildNarrative(events, findings, stats, ctReversal, opts = {}) {
  const sentences = [];
  const config = opts.config || {};
  const asset = config.asset_name;

  // 1) Scope
  let nrec = opts.totalRecords;
  if (nrec == null && stats) nrec = stats._thresholds?.total_records;
  const scopeBits = [];
  scopeBits.push(asset ? `Asset ${asset}` : 'This session');
  if (opts.durationSecs) scopeBits.push(`captured over ${fmtDuration(opts.durationSecs)}`);
  if (nrec) scopeBits.push(`(${nrec.toLocaleString('en-US')} one-second records)`);
  sentences.push(scopeBits.join(' ').trim() + '.');

  // 2) Headline event
  const outages = events.filter((e) => e.kind === 'outage');
  const dips = events.filter((e) => e.kind === 'dip');
  const swells = events.filter((e) => e.kind === 'swell');
  const dur = (e) => (e.tEndMs - e.tStartMs) / 1000;
  if (outages.length) {
    let worst = outages[0];
    for (const o of outages) if (dur(o) > dur(worst)) worst = o;
    const lead = dips.find((d) => {
      const gap = (worst.tStartMs - d.tEndMs) / 1000;
      return gap >= 0 && gap <= 30;
    });
    let ctx = '';
    if (lead) {
      ctx = `, preceded by a phase-${(lead.affectedPhases ?? []).join('/') || '?'} ` +
        `dip to ${(lead.severity * 100).toFixed(0)}%`;
    }
    sentences.push(
      `The most significant event was a ${fmtDuration(dur(worst))} outage at ` +
      `${hhmm(worst.tStartMs)}${ctx}.`);
  } else if (dips.length || swells.length) {
    let worstDip = dips.length ? dips[0] : null;
    for (const d of dips) if (d.severity < worstDip.severity) worstDip = d;
    let worstSwell = swells.length ? swells[0] : null;
    for (const s of swells) if (s.severity > worstSwell.severity) worstSwell = s;
    if (worstDip) {
      sentences.push(
        `No outages occurred; the deepest voltage dip fell to ` +
        `${(worstDip.severity * 100).toFixed(0)}% of nominal on phase(s) ` +
        `${(worstDip.affectedPhases ?? []).join('/') || '?'} at ${hhmm(worstDip.tStartMs)}.`);
    } else if (worstSwell) {
      sentences.push(
        `No outages occurred; the largest swell reached ` +
        `${(worstSwell.severity * 100).toFixed(0)}% of nominal at ${hhmm(worstSwell.tStartMs)}.`);
    }
  } else {
    sentences.push('No outages, dips, or swells were detected.');
  }

  // 3) Power factor / imbalance
  if (stats && stats.PF_total_avg) {
    const pf = stats.PF_total_avg;
    sentences.push(
      `Power factor (total) averaged ${pf.mean.toFixed(2)} ` +
      `(p5 ${pf.p5.toFixed(2)}, p95 ${pf.p95.toFixed(2)}).`);
  }
  const pfFinding = findings.find((f) => f.kind === 'pf_drift');
  if (pfFinding) sentences.push(pfFinding.headline.replace(/\.$/, '') + '.');
  const imbFinding = findings.find((f) =>
    f.kind === 'imbalance_sustained' || f.kind === 'phase_asymmetry');
  if (imbFinding) sentences.push(imbFinding.headline.replace(/\.$/, '') + '.');

  // 4) CT reversal
  if (ctReversal && ctReversal.reversed) {
    const pct = ctReversal.frac_negative * 100;
    sentences.push(
      `WARNING: real power is negative for ${pct.toFixed(0)}% of non-outage time — ` +
      'the iFlex CTs are likely reversed; re-run with --reverse-cts.');
  }

  // 5) Bottom line
  const nAlert = findings.filter((f) => f.severity === 'alert').length;
  if (nAlert) {
    sentences.push(`Bottom line: ${nAlert} alert-level finding(s) warrant follow-up.`);
  } else if (events.length) {
    sentences.push(`Bottom line: ${events.length} event(s) detected; no alert-level findings.`);
  } else {
    sentences.push('Bottom line: the supply looks clean over this capture.');
  }

  return sentences.join(' ');
}

export function narrativeMarkdown(narrative, config = null) {
  const title = (config && config.asset_name) || 'Session';
  return [`# Executive Summary — ${title}`, '', narrative, ''].join('\n');
}
