"""Cross-session insights — patterns that only emerge across multiple captures.

Pure stdlib; mirrors the design of `insights.py` but operates on a list of
sessions. Each session is a dict:

    {
        "label": "before",
        "records": [Record, ...],
        "events": [Event, ...],
        "findings": [Finding, ...],  # single-session findings (optional)
    }

Returns ``list[CompareFinding]``. Same shape as ``Finding`` plus an extra
``session_labels`` tuple naming the sessions the finding spans.

Thresholds live in ``spec/field_map.json`` → ``compare_insight_rules``.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Iterable, Sequence

from .events import Event
from .insights import Finding
from .parser import FIELDS, Record, SPEC


_FIELD_INDEX = {f.name: f.index for f in FIELDS}
_RULES: dict = SPEC.get("compare_insight_rules", {})


def _r(key: str, default: float) -> float:
    v = _RULES.get(key)
    return float(v) if v is not None else default


@dataclass(frozen=True)
class CompareFinding:
    id: int
    kind: str
    severity: str
    headline: str
    detail: str
    session_labels: tuple[str, ...] = ()
    recommended_actions: tuple[str, ...] = ()


def to_jsonable(f: CompareFinding) -> dict:
    d = asdict(f)
    d["session_labels"] = list(f.session_labels)
    d["recommended_actions"] = list(f.recommended_actions)
    return d


# --- Stats helpers (no numpy) ----------------------------------------------

def _linreg(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, float]:
    """Return (slope, intercept, pearson_r). Assumes len(xs) == len(ys) >= 2."""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    slope = sxy / sxx if sxx else 0.0
    intercept = my - slope * mx
    denom = (sxx * syy) ** 0.5
    r = sxy / denom if denom else 0.0
    return slope, intercept, r


def _phase_avg(records: Sequence[Record], col_name: str) -> float:
    idx = _FIELD_INDEX[col_name]
    # Mask outage samples (avg L-N < 50 V on any phase).
    va = _FIELD_INDEX["V_LN_a_avg_V"]
    vb = _FIELD_INDEX["V_LN_b_avg_V"]
    vc = _FIELD_INDEX["V_LN_c_avg_V"]
    vals = [
        r.floats[idx] for r in records
        if r.floats[va] > 50 and r.floats[vb] > 50 and r.floats[vc] > 50
    ]
    return sum(vals) / len(vals) if vals else 0.0


def _session_start_date(records: Sequence[Record]) -> dt.datetime | None:
    return records[0].start if records else None


def _pf_low_fraction(records: Sequence[Record], threshold: float = 0.85) -> float:
    pf_idx = _FIELD_INDEX["PF_total_avg"]
    va = _FIELD_INDEX["V_LN_a_avg_V"]
    vb = _FIELD_INDEX["V_LN_b_avg_V"]
    vc = _FIELD_INDEX["V_LN_c_avg_V"]
    total = 0
    hits = 0
    for r in records:
        if r.floats[va] <= 50 or r.floats[vb] <= 50 or r.floats[vc] <= 50:
            continue
        total += 1
        pf = r.floats[pf_idx]
        if 0 < abs(pf) < threshold:
            hits += 1
    return hits / total if total else 0.0


# --- Individual rules ------------------------------------------------------

def _rule_voltage_drift(sessions: Sequence[dict]) -> list[CompareFinding]:
    if len(sessions) < 2:
        return []
    threshold = _r("voltage_drift_v_per_day", 0.1)
    base = sessions[0]
    base_start = _session_start_date(base["records"])
    if base_start is None:
        return []
    days: list[float] = []
    means_by_phase: dict[str, list[float]] = {"a": [], "b": [], "c": []}
    labels = [s["label"] for s in sessions]
    for s in sessions:
        start = _session_start_date(s["records"])
        if start is None:
            return []
        days.append((start - base_start).total_seconds() / 86400.0)
        for ph in ("a", "b", "c"):
            means_by_phase[ph].append(_phase_avg(s["records"], f"V_LN_{ph}_avg_V"))
    findings: list[CompareFinding] = []
    for ph in ("a", "b", "c"):
        ys = means_by_phase[ph]
        # If all sessions start the same day, regression x is degenerate — bail.
        if max(days) - min(days) < 1e-6:
            continue
        slope, _, _ = _linreg(days, ys)
        if abs(slope) < threshold:
            continue
        direction = "rising" if slope > 0 else "falling"
        sev = "alert" if abs(slope) >= 2 * threshold else "warn"
        findings.append(CompareFinding(
            id=-1, kind="voltage_drift", severity=sev,
            headline=f"Phase {ph.upper()} L-N voltage {direction} {abs(slope):.2f} V/day across captures",
            detail=(
                f"Linear fit of per-session phase {ph.upper()} L-N average vs "
                f"capture date shows a {direction} trend of {abs(slope):.3f} V/day "
                f"over {len(sessions)} session(s). Per-session means: "
                + ", ".join(f"{lbl}: {v:.2f} V" for lbl, v in zip(labels, ys))
                + "."
            ),
            session_labels=tuple(labels),
            recommended_actions=(
                "Verify transformer tap setting and feeder loading hasn't changed.",
                "If drift correlates with seasonal load or weather, may be normal — "
                "compare against the utility's voltage profile.",
            ),
        ))
    return findings


def _rule_recurring_outages(sessions: Sequence[dict]) -> list[CompareFinding]:
    if len(sessions) < 2:
        return []
    window_mins = int(_r("recurring_outage_window_mins", 15))
    # Bucket each outage by time-of-day in minutes; (session_idx, minutes-of-day, event).
    by_bucket: dict[int, list[tuple[str, dt.datetime]]] = {}
    for s in sessions:
        for ev in s.get("events", []):
            if ev.kind != "outage":
                continue
            tod_mins = ev.t_start.hour * 60 + ev.t_start.minute
            bucket = (tod_mins // window_mins) * window_mins
            by_bucket.setdefault(bucket, []).append((s["label"], ev.t_start))
    findings: list[CompareFinding] = []
    for bucket, hits in by_bucket.items():
        sessions_hit = {label for label, _ in hits}
        if len(sessions_hit) < 2:
            continue
        hh = bucket // 60
        mm = bucket % 60
        findings.append(CompareFinding(
            id=-1, kind="recurring_outages",
            severity="warn" if len(sessions_hit) < len(sessions) else "alert",
            headline=(
                f"Recurring outages around {hh:02d}:{mm:02d} in "
                f"{len(sessions_hit)} of {len(sessions)} captures"
            ),
            detail=(
                f"Outage events occurred within the same {window_mins}-min "
                f"window of the day across {len(sessions_hit)} different "
                "sessions. This pattern often points to a scheduled load, an "
                "external switching event, or upstream maintenance. "
                "Occurrences: "
                + "; ".join(f"{lbl} at {t.isoformat()}" for lbl, t in hits)
                + "."
            ),
            session_labels=tuple(sorted(sessions_hit)),
            recommended_actions=(
                "Cross-reference the timestamps with utility planned-work logs.",
                "If on-site, check whether automation (HVAC bypass, EV charger "
                "scheduling, etc.) coincides with the window.",
            ),
        ))
    return findings


def _rule_pf_degradation(sessions: Sequence[dict]) -> list[CompareFinding]:
    min_sessions = int(_r("pf_degradation_min_sessions", 3))
    if len(sessions) < min_sessions:
        return []
    min_r = _r("pf_degradation_min_r", 0.5)
    base_start = _session_start_date(sessions[0]["records"])
    if base_start is None:
        return []
    days: list[float] = []
    fracs: list[float] = []
    labels: list[str] = []
    for s in sessions:
        start = _session_start_date(s["records"])
        if start is None:
            return []
        days.append((start - base_start).total_seconds() / 86400.0)
        fracs.append(_pf_low_fraction(s["records"]))
        labels.append(s["label"])
    if max(days) - min(days) < 1e-6:
        return []
    slope, _, r = _linreg(days, fracs)
    if r < min_r or slope <= 0:
        return []
    return [CompareFinding(
        id=-1, kind="pf_degradation", severity="warn",
        headline=(
            f"Power factor degrading over time "
            f"(slope {slope:.4f}/day, r={r:.2f})"
        ),
        detail=(
            f"The fraction of operating time with |PF| < 0.85 is trending up "
            f"across the {len(sessions)} captured sessions (Pearson r={r:.2f}). "
            "Per-session low-PF fractions: "
            + ", ".join(f"{lbl}: {f * 100:.1f}%" for lbl, f in zip(labels, fracs))
            + "."
        ),
        session_labels=tuple(labels),
        recommended_actions=(
            "Investigate whether new inductive load has been added.",
            "Re-evaluate PFC sizing — the headroom in the original spec may "
            "have been consumed.",
        ),
    )]


def _rule_stiffness_emergence(sessions: Sequence[dict]) -> list[CompareFinding]:
    if len(sessions) < 2:
        return []
    # Sort sessions by start date; we want "older→newer" trajectory.
    enriched = []
    for s in sessions:
        start = _session_start_date(s["records"])
        if start is None:
            return []
        enriched.append((start, s))
    enriched.sort(key=lambda x: x[0])
    sorted_sessions = [s for _, s in enriched]
    has_stiff = [
        any(f.kind == "freq_source_stiffness" for f in s.get("findings", []))
        for s in sorted_sessions
    ]
    # Emergence pattern: not present in first half, present in last half.
    half = len(has_stiff) // 2
    if half == 0:
        return []
    earlier_any = any(has_stiff[:half])
    later_count = sum(has_stiff[half:])
    if earlier_any or later_count == 0:
        return []
    older_labels = [s["label"] for s in sorted_sessions[:half]]
    newer_labels = [s["label"] for s in sorted_sessions[half:]]
    return [CompareFinding(
        id=-1, kind="source_stiffness_emergence", severity="warn",
        headline=(
            f"Weak-source signature emerged in {later_count} of "
            f"{len(has_stiff) - half} recent captures"
        ),
        detail=(
            f"Earlier captures ({', '.join(older_labels)}) did not show the "
            "freq_source_stiffness finding, but recent captures "
            f"({', '.join(newer_labels)}) do. This suggests the upstream "
            "supply impedance has grown — typical after a feeder reconfiguration, "
            "transformer tap change, or generator-mode switchover."
        ),
        session_labels=tuple(s["label"] for s in sorted_sessions),
        recommended_actions=(
            "Confirm the supply topology hasn't changed.",
            "If on a microgrid or genset, check governor/droop settings.",
        ),
    )]


def _rule_event_count_trend(sessions: Sequence[dict]) -> list[CompareFinding]:
    min_sessions = int(_r("event_trend_min_sessions", 3))
    if len(sessions) < min_sessions:
        return []
    min_r = _r("event_trend_min_r", 0.7)
    base_start = _session_start_date(sessions[0]["records"])
    if base_start is None:
        return []
    days: list[float] = []
    labels: list[str] = []
    counts_by_kind: dict[str, list[int]] = {}
    for s in sessions:
        start = _session_start_date(s["records"])
        if start is None:
            return []
        days.append((start - base_start).total_seconds() / 86400.0)
        labels.append(s["label"])
        kinds_here: dict[str, int] = {}
        for ev in s.get("events", []):
            kinds_here[ev.kind] = kinds_here.get(ev.kind, 0) + 1
        for k in kinds_here:
            counts_by_kind.setdefault(k, [])
        # Pad zeros for kinds we already track but absent this session
    # Backfill zero counts for missing kinds per session
    all_kinds = sorted(counts_by_kind.keys())
    counts_by_kind = {k: [] for k in all_kinds}
    for s in sessions:
        local = {}
        for ev in s.get("events", []):
            local[ev.kind] = local.get(ev.kind, 0) + 1
        for k in all_kinds:
            counts_by_kind[k].append(local.get(k, 0))
    if max(days) - min(days) < 1e-6:
        return []
    findings: list[CompareFinding] = []
    for k, counts in counts_by_kind.items():
        if sum(counts) < 2:
            continue
        slope, _, r = _linreg(days, counts)
        if abs(r) < min_r or abs(slope) < 0.01:
            continue
        direction = "rising" if slope > 0 else "falling"
        findings.append(CompareFinding(
            id=-1, kind=f"event_trend_{k}",
            severity="warn" if slope > 0 else "info",
            headline=(
                f"{k} events {direction} {abs(slope):.2f}/day "
                f"(r={r:.2f})"
            ),
            detail=(
                f"Linear trend of {k} event count across captures: "
                + ", ".join(f"{lbl}: {n}" for lbl, n in zip(labels, counts))
                + f". Pearson r = {r:.2f}, slope = {slope:.3f} per day."
            ),
            session_labels=tuple(labels),
        ))
    return findings


# --- Public API -----------------------------------------------------------

_SEV_RANK = {"alert": 0, "warn": 1, "info": 2}


def analyze_compare(sessions: Sequence[dict]) -> list[CompareFinding]:
    """Run every cross-session rule. Returns ordered CompareFindings."""
    raw: list[CompareFinding] = []
    raw.extend(_rule_voltage_drift(sessions))
    raw.extend(_rule_recurring_outages(sessions))
    raw.extend(_rule_pf_degradation(sessions))
    raw.extend(_rule_stiffness_emergence(sessions))
    raw.extend(_rule_event_count_trend(sessions))
    raw.sort(key=lambda f: (_SEV_RANK.get(f.severity, 99), f.kind))
    return [
        CompareFinding(
            id=i, kind=f.kind, severity=f.severity,
            headline=f.headline, detail=f.detail,
            session_labels=f.session_labels,
            recommended_actions=f.recommended_actions,
        )
        for i, f in enumerate(raw)
    ]
