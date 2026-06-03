"""Auto-narrative / executive summary (Feature E).

Rule-based plain-English summary built from detected events, insights, whole-
session stats, and the CT-reversal check — no LLM. Deterministic so the same
session always yields the same prose, and so the JS port can match it exactly.

The narrative is a small ordered set of sentences:
  1. Scope (asset, duration, record count).
  2. Headline event (worst outage / dip / swell), with context.
  3. Power-factor / imbalance summary from stats + insights.
  4. CT-reversal warning if flagged.
  5. A one-line bottom line.
"""
from __future__ import annotations

import datetime as dt
from typing import Sequence


def _fmt_duration(secs: float) -> str:
    secs = int(round(secs))
    if secs >= 3600:
        return f"{secs / 3600:.1f} h"
    if secs >= 60:
        return f"{secs / 60:.1f} min"
    return f"{secs} s"


def _hhmm(iso_or_dt) -> str:
    if isinstance(iso_or_dt, str):
        try:
            d = dt.datetime.fromisoformat(iso_or_dt)
        except ValueError:
            return iso_or_dt
    else:
        d = iso_or_dt
    return d.strftime("%Y-%m-%d %H:%M UTC")


def build_narrative(
    events: Sequence,
    findings: Sequence,
    stats: dict | None,
    ct_reversal: dict | None,
    config: dict | None = None,
    total_records: int | None = None,
    duration_secs: float | None = None,
) -> str:
    """Return a deterministic plain-English executive summary string.

    ``events`` are Event objects (id/kind/t_start/t_end/severity/affected_phases),
    ``findings`` are Finding objects (kind/severity/headline), ``stats`` is the
    whole_session_stats dict, ``ct_reversal`` is detect_ct_reversal output.
    """
    sentences: list[str] = []
    config = config or {}
    asset = config.get("asset_name")

    # 1) Scope
    nrec = total_records
    if nrec is None and stats:
        nrec = stats.get("_thresholds", {}).get("total_records")
    scope_bits = []
    if asset:
        scope_bits.append(f"Asset {asset}")
    else:
        scope_bits.append("This session")
    if duration_secs:
        scope_bits.append(f"captured over {_fmt_duration(duration_secs)}")
    if nrec:
        scope_bits.append(f"({nrec:,} one-second records)")
    sentences.append(" ".join(scope_bits).strip() + ".")

    # 2) Headline event
    outages = [e for e in events if e.kind == "outage"]
    dips = [e for e in events if e.kind == "dip"]
    swells = [e for e in events if e.kind == "swell"]
    if outages:
        worst = max(outages, key=lambda e: (e.t_end - e.t_start).total_seconds())
        dur = (worst.t_end - worst.t_start).total_seconds()
        # Context: leading dip / restoration inrush within 30 s.
        lead = next((d for d in dips
                     if 0 <= (worst.t_start - d.t_end).total_seconds() <= 30), None)
        ctx = ""
        if lead:
            ctx = (f", preceded by a phase-{'/'.join(lead.affected_phases) or '?'} "
                   f"dip to {lead.severity * 100:.0f}%")
        sentences.append(
            f"The most significant event was a {_fmt_duration(dur)} outage at "
            f"{_hhmm(worst.t_start)}{ctx}.")
    elif dips or swells:
        worst_dip = min(dips, key=lambda e: e.severity, default=None)
        worst_swell = max(swells, key=lambda e: e.severity, default=None)
        if worst_dip:
            sentences.append(
                f"No outages occurred; the deepest voltage dip fell to "
                f"{worst_dip.severity * 100:.0f}% of nominal on phase(s) "
                f"{'/'.join(worst_dip.affected_phases) or '?'} at "
                f"{_hhmm(worst_dip.t_start)}.")
        elif worst_swell:
            sentences.append(
                f"No outages occurred; the largest swell reached "
                f"{worst_swell.severity * 100:.0f}% of nominal at "
                f"{_hhmm(worst_swell.t_start)}.")
    else:
        sentences.append("No outages, dips, or swells were detected.")

    # 3) Power factor / imbalance from stats + findings
    if stats and "PF_total_avg" in stats:
        pf = stats["PF_total_avg"]
        sentences.append(
            f"Power factor (total) averaged {pf['mean']:.2f} "
            f"(p5 {pf['p5']:.2f}, p95 {pf['p95']:.2f}).")
    pf_finding = next((f for f in findings if f.kind == "pf_drift"), None)
    if pf_finding:
        sentences.append(pf_finding.headline.rstrip(".") + ".")
    imb_finding = next((f for f in findings if f.kind in
                        ("imbalance_sustained", "phase_asymmetry")), None)
    if imb_finding:
        sentences.append(imb_finding.headline.rstrip(".") + ".")

    # 4) CT reversal
    if ct_reversal and ct_reversal.get("reversed"):
        pct = ct_reversal["frac_negative"] * 100
        sentences.append(
            f"WARNING: real power is negative for {pct:.0f}% of non-outage time — "
            "the iFlex CTs are likely reversed; re-run with --reverse-cts.")

    # 5) Bottom line
    n_alert = sum(1 for f in findings if getattr(f, "severity", "") == "alert")
    if n_alert:
        sentences.append(
            f"Bottom line: {n_alert} alert-level finding(s) warrant follow-up.")
    elif events:
        sentences.append(
            f"Bottom line: {len(events)} event(s) detected; no alert-level findings.")
    else:
        sentences.append("Bottom line: the supply looks clean over this capture.")

    return " ".join(sentences)


def narrative_markdown(narrative: str, config: dict | None = None) -> str:
    """Wrap the narrative as a small markdown document for narrative.md."""
    config = config or {}
    title = config.get("asset_name") or "Session"
    lines = [f"# Executive Summary — {title}", "", narrative, ""]
    return "\n".join(lines)
