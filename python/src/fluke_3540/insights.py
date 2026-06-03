"""Insights engine — cross-correlate events, snapshots, and the per-second
series into human-readable Findings.

Pure stdlib (no numpy). Thresholds live in ``spec/field_map.json`` →
``insight_rules`` so the JavaScript port reads the same values.

Rules (each described in detail in ``docs/INSIGHTS.md``):

- ``outage_signature``: outage event ± dip and/or current-spike inside
  ``outage_dip_window_secs`` of either edge — characterises the disturbance
  shape (loss-of-supply vs. brown-out, with or without inrush on restore).
- ``phase_asymmetry``: per-phase avg L-N voltage spread > ``phase_asymmetry_pct``.
- ``pf_drift``: |PF_total_avg| < ``pf_drift_threshold`` for at least
  ``pf_drift_min_fraction`` of non-outage time; recommends PFC kVAR sizing.
- ``imbalance_sustained``: NEMA imbalance > ``imbalance_sustained_pct``
  sustained for ``imbalance_sustained_secs`` seconds outside an outage.
- ``freq_source_stiffness``: ``freq_stiffness_min_count`` or more power_step
  events coincide with a freq deviation > ``freq_stiffness_hz``.
- ``outage_frequency``: > ``outage_frequency_per_day`` outage events / day.
- ``current_spike_ratio``: peak phase current > ``current_to_mean_ratio_alert``
  × mean current — flags transient inrush vs. continuous load.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Iterable, Sequence

from .events import Event
from .parser import SPEC
from .snapshots import Snapshot
from .store import ColumnStore


_RULES: dict = SPEC.get("insight_rules", {})


def _r(key: str, default: float) -> float:
    """Read a rule threshold with a sensible default."""
    v = _RULES.get(key)
    return float(v) if v is not None else default


@dataclass(frozen=True)
class Finding:
    id: int
    kind: str
    severity: str  # "info" | "warn" | "alert"
    headline: str
    detail: str
    related_event_ids: tuple[int, ...] = ()
    recommended_actions: tuple[str, ...] = ()


def to_jsonable(f: Finding) -> dict:
    d = asdict(f)
    d["related_event_ids"] = list(f.related_event_ids)
    d["recommended_actions"] = list(f.recommended_actions)
    return d


# --- Helpers ----------------------------------------------------------------

def _col(store: ColumnStore, name: str):
    """Return the store column (array.array). Indexing/slicing behave as a list."""
    return store.col(name)


def _non_outage_mask(store: ColumnStore) -> list[bool]:
    """All-three-phase avg voltage > outage threshold."""
    a = store.col("V_LN_a_avg_V")
    b = store.col("V_LN_b_avg_V")
    c = store.col("V_LN_c_avg_V")
    return [a[i] > 50.0 and b[i] > 50.0 and c[i] > 50.0 for i in range(store.n)]


def _mean(values: Iterable[float]) -> float:
    vs = list(values)
    return sum(vs) / len(vs) if vs else 0.0


def _fraction(mask: Iterable[bool]) -> float:
    total = 0
    hits = 0
    for m in mask:
        total += 1
        if m:
            hits += 1
    return hits / total if total else 0.0


# --- Individual rules -------------------------------------------------------

def _rule_outage_signatures(events: Sequence[Event]) -> list[Finding]:
    window = dt.timedelta(seconds=_r("outage_dip_window_secs", 30))
    findings: list[Finding] = []
    for ev in events:
        if ev.kind != "outage":
            continue
        related = [ev.id]
        bits: list[str] = []
        # Look for a leading dip
        leading = next(
            (e for e in events
             if e.kind == "dip" and e.t_end >= ev.t_start - window
             and e.t_end <= ev.t_start),
            None,
        )
        if leading is not None:
            related.append(leading.id)
            bits.append(
                f"a leading dip on phase(s) {'/'.join(leading.affected_phases) or '?'} "
                f"({leading.severity * 277:.0f} V)"
            )
        # Trailing high-current event (restoration inrush)
        trailing = next(
            (e for e in events
             if e.kind == "high_current" and e.t_start >= ev.t_end
             and e.t_start <= ev.t_end + window),
            None,
        )
        if trailing is not None:
            related.append(trailing.id)
            bits.append(
                f"restoration inrush on phase {trailing.affected_phases[0]} "
                f"({trailing.severity:.0f} A)"
            )
        # Trailing dip (often happens on reclose)
        trailing_dip = next(
            (e for e in events
             if e.kind == "dip" and e.t_start >= ev.t_end
             and e.t_start <= ev.t_end + window),
            None,
        )
        if trailing_dip is not None and trailing_dip.id not in related:
            related.append(trailing_dip.id)
            bits.append(
                f"a trailing dip on phase(s) {'/'.join(trailing_dip.affected_phases) or '?'}"
            )

        dur = int((ev.t_end - ev.t_start).total_seconds())
        headline = f"Outage at {ev.t_start.strftime('%H:%M:%S')} ({dur} s)"
        if bits:
            detail = (
                f"Outage at {ev.t_start.isoformat()} lasted {dur} s, "
                f"with {', '.join(bits)}."
            )
        else:
            detail = (
                f"Outage at {ev.t_start.isoformat()} lasted {dur} s. "
                "No leading dip or restoration inrush was detected in the "
                f"{int(window.total_seconds())}-s windows around the edges, "
                "which is consistent with a hard supply loss."
            )
        sev = "alert" if dur >= 60 else "warn"
        findings.append(Finding(
            id=-1, kind="outage_signature", severity=sev,
            headline=headline, detail=detail,
            related_event_ids=tuple(related),
            recommended_actions=(
                "Correlate with the utility's outage records or with on-site "
                "switchgear logs.",
                "If inrush was observed, verify that downstream breakers and "
                "transformer protection hold up under the recorded current.",
            ),
        ))
    return findings


def _rule_phase_asymmetry(store: ColumnStore) -> list[Finding]:
    threshold_pct = _r("phase_asymmetry_pct", 2.0)
    not_outage = _non_outage_mask(store)
    a = _col(store, "V_LN_a_avg_V")
    b = _col(store, "V_LN_b_avg_V")
    c = _col(store, "V_LN_c_avg_V")
    a_vals = [a[i] for i in range(store.n) if not_outage[i]]
    b_vals = [b[i] for i in range(store.n) if not_outage[i]]
    c_vals = [c[i] for i in range(store.n) if not_outage[i]]
    if not a_vals:
        return []
    means = {"a": _mean(a_vals), "b": _mean(b_vals), "c": _mean(c_vals)}
    overall = _mean(means.values())
    spread_pct = (max(means.values()) - min(means.values())) / overall * 100
    if spread_pct < threshold_pct:
        return []
    hottest = max(means, key=lambda k: means[k])
    coolest = min(means, key=lambda k: means[k])
    headline = (
        f"Phase asymmetry {spread_pct:.1f}% across the session "
        f"(phase {hottest.upper()} hottest)"
    )
    detail = (
        f"Per-phase L-N voltage average across non-outage samples: "
        f"A={means['a']:.1f} V, B={means['b']:.1f} V, C={means['c']:.1f} V. "
        f"That is {spread_pct:.2f}% spread relative to the mean "
        f"({overall:.1f} V). Phase {hottest.upper()} runs hottest; "
        f"phase {coolest.upper()} runs coldest."
    )
    sev = "alert" if spread_pct >= 3.0 else "warn"
    return [Finding(
        id=-1, kind="phase_asymmetry", severity=sev,
        headline=headline, detail=detail,
        recommended_actions=(
            "Check transformer tap setting.",
            "Review per-phase load balance (large single-phase loads concentrated on "
            "one phase often produce this).",
            "If asymmetry persists across multiple captures, schedule a thermography "
            "scan of the affected feeders.",
        ),
    )]


def _rule_pf_drift(store: ColumnStore) -> list[Finding]:
    threshold = _r("pf_drift_threshold", 0.85)
    min_frac = _r("pf_drift_min_fraction", 0.10)
    not_outage = _non_outage_mask(store)
    pf = _col(store, "PF_total_avg")
    s = _col(store, "S_total_avg_VA")
    q = _col(store, "Q_total_avg_VAR")
    low_pf_mask = [
        not_outage[i] and 0 < abs(pf[i]) < threshold
        for i in range(store.n)
    ]
    frac = _fraction(low_pf_mask)
    if frac < min_frac:
        return []
    # Mean Q during low-PF periods — a rough PFC kVAR sizing recommendation.
    q_during = [abs(q[i]) for i in range(store.n) if low_pf_mask[i]]
    s_during = [abs(s[i]) for i in range(store.n) if low_pf_mask[i]]
    rec_kvar = int(round(_mean(q_during) / 1000)) if q_during else 0
    avg_s_kva = _mean(s_during) / 1000 if s_during else 0
    headline = (
        f"Power factor below {threshold:.2f} for "
        f"{frac * 100:.1f}% of non-outage time"
    )
    detail = (
        f"True PF dipped under {threshold:.2f} on the total channel for "
        f"{frac * 100:.1f}% of operating time. Average apparent power during "
        f"those periods was {avg_s_kva:.1f} kVA with about "
        f"{rec_kvar} kVAR of reactive demand. A correctly-sized power-factor "
        "correction bank would recover most of that as reduced current draw."
    )
    sev = "warn" if frac < 0.30 else "alert"
    actions = [
        f"Consider a fixed or stepped PFC bank in the {max(rec_kvar, 5)} kVAR class "
        "(verify with full kVAR distribution before committing).",
        "Re-measure after correction to confirm PF rises above the threshold.",
        "If the load varies widely, a stepped bank or active PFC unit may be needed.",
    ]
    return [Finding(
        id=-1, kind="pf_drift", severity=sev,
        headline=headline, detail=detail,
        recommended_actions=tuple(actions),
    )]


def _rule_imbalance_sustained(store: ColumnStore) -> list[Finding]:
    pct_threshold = _r("imbalance_sustained_pct", 1.5)
    secs_threshold = int(_r("imbalance_sustained_secs", 60))
    not_outage = _non_outage_mask(store)
    a = _col(store, "V_LN_a_avg_V")
    b = _col(store, "V_LN_b_avg_V")
    c = _col(store, "V_LN_c_avg_V")
    # Compute per-sample imbalance
    imbal = []
    for i in range(store.n):
        if not not_outage[i]:
            imbal.append(0.0)
            continue
        m = (a[i] + b[i] + c[i]) / 3.0
        if m <= 50.0:
            imbal.append(0.0)
            continue
        imbal.append((max(a[i], b[i], c[i]) - min(a[i], b[i], c[i])) / m * 100)
    # Find runs above the threshold
    sustained_runs: list[tuple[int, int]] = []
    in_run = False
    start = 0
    for i, v in enumerate(imbal):
        if v > pct_threshold and not_outage[i]:
            if not in_run:
                in_run = True
                start = i
        else:
            if in_run:
                if i - start >= secs_threshold:
                    sustained_runs.append((start, i - 1))
                in_run = False
    if in_run and len(imbal) - start >= secs_threshold:
        sustained_runs.append((start, len(imbal) - 1))
    if not sustained_runs:
        return []
    total_secs = sum(e - s + 1 for s, e in sustained_runs)
    peak_pct = max(imbal[s:e + 1] and max(imbal[s:e + 1]) for s, e in sustained_runs)
    headline = (
        f"Imbalance > {pct_threshold:.1f}% sustained for "
        f"{total_secs} s total ({len(sustained_runs)} window(s))"
    )
    detail = (
        f"NEMA voltage imbalance exceeded {pct_threshold:.1f}% for "
        f"{total_secs} cumulative seconds across {len(sustained_runs)} "
        f"window(s) of at least {secs_threshold} s. Peak imbalance during "
        f"those windows was {peak_pct:.2f}%."
    )
    sev = "warn"
    return [Finding(
        id=-1, kind="imbalance_sustained", severity=sev,
        headline=headline, detail=detail,
        recommended_actions=(
            "Identify the single-phase loads driving the imbalance and rebalance "
            "across phases if possible.",
            "Sustained imbalance derates motors by approximately "
            "(2 × imbalance_pct)^2 — verify motor nameplates and thermal margin.",
        ),
    )]


def _rule_freq_stiffness(store: ColumnStore, events: Sequence[Event]) -> list[Finding]:
    threshold_hz = _r("freq_stiffness_hz", 0.05)
    min_count = int(_r("freq_stiffness_min_count", 3))
    freq = _col(store, "freq_avg_Hz")
    record_index_by_time = {t: i for i, t in enumerate(store.iter_times())}
    correlations: list[Event] = []
    for ev in events:
        if ev.kind != "power_step":
            continue
        # Find the index nearest the event time
        i = record_index_by_time.get(ev.t_start)
        if i is None:
            continue
        window_start = max(0, i - 2)
        window_end = min(len(freq) - 1, i + 2)
        max_dev = max(abs(freq[k] - 60.0) for k in range(window_start, window_end + 1))
        if max_dev > threshold_hz:
            correlations.append(ev)
    if len(correlations) < min_count:
        return []
    headline = (
        f"{len(correlations)} power steps correlated with freq deviation "
        f"> {threshold_hz:.2f} Hz"
    )
    detail = (
        f"Of the {sum(1 for e in events if e.kind == 'power_step')} detected "
        f"power_step events, {len(correlations)} coincided with a line-frequency "
        f"deviation greater than {threshold_hz:.2f} Hz inside a ±2-second window. "
        "This is consistent with a weak source: the upstream impedance is high "
        "enough that load changes pull the frequency around."
    )
    return [Finding(
        id=-1, kind="freq_source_stiffness", severity="warn",
        headline=headline, detail=detail,
        related_event_ids=tuple(e.id for e in correlations),
        recommended_actions=(
            "If the site is on a generator or microgrid, review droop / governor settings.",
            "On a grid-connected site, this often points to a long radial feeder; "
            "raising the conductor size or moving large loads closer to a stiffer "
            "node can help.",
        ),
    )]


def _rule_outage_frequency(store: ColumnStore, events: Sequence[Event]) -> list[Finding]:
    per_day_threshold = _r("outage_frequency_per_day", 1.0)
    outages = [e for e in events if e.kind == "outage"]
    if not outages or store.n == 0:
        return []
    duration_secs = (store.last_end - store.first_start).total_seconds()
    if duration_secs <= 0:
        return []
    duration_days = duration_secs / 86400.0
    rate = len(outages) / duration_days
    if rate < per_day_threshold:
        return []
    sev = "alert" if rate >= 2 * per_day_threshold else "warn"
    headline = (
        f"{len(outages)} outage(s) in {duration_days:.2f} day(s) "
        f"({rate:.2f}/day)"
    )
    detail = (
        f"The session captured {len(outages)} outage events over "
        f"{duration_days:.2f} days, a rate of {rate:.2f} per day. "
        "Most utility benchmarks treat anything above one customer outage per "
        "year as poor reliability for industrial feeders."
    )
    return [Finding(
        id=-1, kind="outage_frequency", severity=sev,
        headline=headline, detail=detail,
        related_event_ids=tuple(e.id for e in outages),
        recommended_actions=(
            "Open a service ticket with the utility citing the timestamps.",
            "If outages are sustained, consider a UPS for control loops or a "
            "soft-shutdown sequence for sensitive equipment.",
        ),
    )]


def _rule_current_spike_ratio(store: ColumnStore) -> list[Finding]:
    ratio_threshold = _r("current_to_mean_ratio_alert", 5.0)
    findings: list[Finding] = []
    not_outage = _non_outage_mask(store)
    for phase in ("a", "b", "c"):
        i_max = _col(store, f"I_{phase}_max_A")
        valid = [i_max[i] for i in range(store.n) if not_outage[i]]
        if not valid:
            continue
        mean = _mean(valid)
        peak = max(valid)
        if mean <= 0:
            continue
        ratio = peak / mean
        if ratio < ratio_threshold:
            continue
        sev = "info"
        headline = (
            f"Phase {phase.upper()} peak current {peak:.0f} A is "
            f"{ratio:.1f}× the steady mean ({mean:.0f} A)"
        )
        detail = (
            f"Phase {phase.upper()} sustained an average current of {mean:.1f} A "
            f"during non-outage operation but reached a peak of {peak:.1f} A. "
            f"A {ratio:.1f}× peak-to-mean ratio is typical for motor starts or "
            "restoration inrush — verify it stays within the upstream breaker's "
            "instantaneous trip curve."
        )
        findings.append(Finding(
            id=-1, kind="current_spike_ratio", severity=sev,
            headline=headline, detail=detail,
            recommended_actions=(
                "Compare the peak against the breaker's instantaneous trip multiple.",
                "If this is a recurring motor inrush, soft-starter or VFD retrofits "
                "can cut peak current to ~2-3× nominal.",
            ),
        ))
    return findings


# --- Public API -------------------------------------------------------------

def analyze(
    records_or_store: Sequence | ColumnStore,
    events: Sequence[Event],
    snapshots: Sequence[Snapshot] | None = None,
    config: dict | None = None,
) -> list[Finding]:
    """Run every rule against the given session data and return ordered Findings.

    Accepts a :class:`~fluke_3540.store.ColumnStore` or an iterable of Records.
    """
    store = (records_or_store if isinstance(records_or_store, ColumnStore)
             else ColumnStore.from_records(records_or_store))
    raw: list[Finding] = []
    raw.extend(_rule_outage_signatures(events))
    raw.extend(_rule_phase_asymmetry(store))
    raw.extend(_rule_pf_drift(store))
    raw.extend(_rule_imbalance_sustained(store))
    raw.extend(_rule_freq_stiffness(store, events))
    raw.extend(_rule_outage_frequency(store, events))
    raw.extend(_rule_current_spike_ratio(store))
    # Order: alert > warn > info, then by kind alphabetical for stability.
    sev_rank = {"alert": 0, "warn": 1, "info": 2}
    raw.sort(key=lambda f: (sev_rank.get(f.severity, 99), f.kind))
    return [
        Finding(
            id=i, kind=f.kind, severity=f.severity,
            headline=f.headline, detail=f.detail,
            related_event_ids=f.related_event_ids,
            recommended_actions=f.recommended_actions,
        )
        for i, f in enumerate(raw)
    ]
