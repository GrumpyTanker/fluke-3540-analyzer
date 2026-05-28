"""Event detection for Fluke 3540 FC sessions.

Detects outages, voltage dips/swells, high-current excursions, frequency
deviations, imbalance spikes, and large power steps. Stdlib only — no numpy.

Thresholds live in EVENT_RULES and follow IEEE 1159 / NEMA conventions.
See docs/EVENT_RULES.md for the rationale.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from statistics import median, mean, pstdev
from typing import Iterable, Sequence

from .parser import FIELDS, Record


# --- Field index lookup -------------------------------------------------------

_FIELD_INDEX = {f.name: f.index for f in FIELDS}


def _idx(name: str) -> int:
    try:
        return _FIELD_INDEX[name]
    except KeyError as e:
        raise KeyError(
            f"Field {name!r} missing from spec/field_map.json — events module "
            "depends on it"
        ) from e


# Voltage channels (per-phase L-N min/max/avg)
_V_LN_MIN = (_idx("V_LN_a_min_V"), _idx("V_LN_b_min_V"), _idx("V_LN_c_min_V"))
_V_LN_MAX = (_idx("V_LN_a_max_V"), _idx("V_LN_b_max_V"), _idx("V_LN_c_max_V"))
_V_LN_AVG = (_idx("V_LN_a_avg_V"), _idx("V_LN_b_avg_V"), _idx("V_LN_c_avg_V"))

# Current channels
_I_MAX = (_idx("I_a_max_A"), _idx("I_b_max_A"), _idx("I_c_max_A"))

# Frequency (use avg)
_FREQ_AVG = _idx("freq_avg_Hz")

# Total active power (avg over 1-sec window)
_P_TOTAL_AVG = _idx("P_total_avg_W")

_PHASES = ("a", "b", "c")


# --- Rule constants -----------------------------------------------------------

@dataclass(frozen=True)
class EventRules:
    """Tunable detection thresholds. Defaults follow plan + IEEE 1159 / NEMA."""
    outage_v_threshold: float = 50.0          # any phase L-N below this V is "outage"
    dip_pct_of_nominal: float = 0.90          # < 90% nominal = dip
    swell_pct_of_nominal: float = 1.10        # > 110% nominal = swell
    high_current_sigma: float = 2.0           # mean + 2σ on any phase
    freq_excursion_hz: float = 0.5            # |f - 60| > 0.5 Hz
    imbalance_pct_threshold: float = 2.5      # NEMA % imbalance threshold
    power_step_pct_of_mean: float = 0.50      # ΔP within 1 sec > 50% session mean |P|
    min_duration_secs: int = 1                # below this an event is ignored
    gap_tolerance_secs: int = 1               # merge runs separated by ≤ this gap
    nominal_freq_hz: float = 60.0


DEFAULT_RULES = EventRules()


@dataclass(frozen=True)
class Event:
    id: int
    kind: str             # outage | dip | swell | high_current | freq_excursion |
                          # imbalance_spike | power_step
    t_start: dt.datetime
    t_end: dt.datetime
    severity: float       # kind-specific magnitude (see comments by each detector)
    affected_phases: tuple[str, ...]


# --- Helpers ------------------------------------------------------------------

def _group_runs(mask: Sequence[bool], gap_tolerance: int) -> list[tuple[int, int]]:
    """Return contiguous [start, end] index ranges where mask is True.
    Runs separated by ≤ gap_tolerance False samples are merged.
    """
    runs: list[tuple[int, int]] = []
    in_run = False
    start = 0
    gap = 0
    for i, m in enumerate(mask):
        if m:
            if not in_run:
                in_run = True
                start = i
            gap = 0
        else:
            if in_run:
                gap += 1
                if gap > gap_tolerance:
                    runs.append((start, i - gap))
                    in_run = False
                    gap = 0
    if in_run:
        runs.append((start, len(mask) - 1 - max(0, gap)))
    return runs


def _infer_nominal_ln_v(v_avg_by_phase: list[list[float]],
                        outage_threshold: float) -> float:
    """Median of per-phase avg L-N voltages, with outage samples excluded."""
    pooled = [
        v
        for phase_vals in v_avg_by_phase
        for v in phase_vals
        if v > outage_threshold and not math.isnan(v)
    ]
    if not pooled:
        raise ValueError("No non-outage voltage samples — cannot infer nominal V_LN")
    return median(pooled)


# --- Main entry point ---------------------------------------------------------

def detect_events(
    records: Iterable[Record],
    nominal_ln_v: float | None = None,
    rules: EventRules = DEFAULT_RULES,
) -> list[Event]:
    """Scan a sequence of Records and return detected events in time order.

    If nominal_ln_v is None, it is auto-inferred as the median of per-phase L-N
    avg voltages with outage samples (<= outage_v_threshold) excluded.
    """
    recs = list(records)
    if not recs:
        return []

    # Column extraction
    times = [r.start for r in recs]
    end_times = [r.end for r in recs]
    v_min = [[r.floats[idx] for r in recs] for idx in _V_LN_MIN]
    v_max = [[r.floats[idx] for r in recs] for idx in _V_LN_MAX]
    v_avg = [[r.floats[idx] for r in recs] for idx in _V_LN_AVG]
    i_max = [[r.floats[idx] for r in recs] for idx in _I_MAX]
    freq = [r.floats[_FREQ_AVG] for r in recs]
    p_total = [r.floats[_P_TOTAL_AVG] for r in recs]

    if nominal_ln_v is None:
        nominal_ln_v = _infer_nominal_ln_v(v_avg, rules.outage_v_threshold)
    dip_threshold = nominal_ln_v * rules.dip_pct_of_nominal
    swell_threshold = nominal_ln_v * rules.swell_pct_of_nominal

    # not_outage[i] is True when ALL phases > outage_v_threshold (avg voltage)
    not_outage = [
        all(v_avg[p][i] > rules.outage_v_threshold for p in range(3))
        for i in range(len(recs))
    ]
    outage_mask = [not n for n in not_outage]

    events: list[Event] = []
    next_id = [0]

    def emit(kind: str, s: int, e: int, severity: float, phases: Iterable[str]):
        if (e - s + 1) < rules.min_duration_secs:
            return
        events.append(Event(
            id=next_id[0], kind=kind,
            t_start=times[s], t_end=end_times[e],
            severity=float(severity),
            affected_phases=tuple(phases),
        ))
        next_id[0] += 1

    # --- outage --------------------------------------------------------------
    # Severity: deepest min L-N voltage seen during the window (lower = worse).
    for s, e in _group_runs(outage_mask, rules.gap_tolerance_secs):
        lows = [min(v_min[p][s:e + 1]) for p in range(3)]
        emit("outage", s, e, severity=min(lows), phases=_PHASES)

    # --- dip ----------------------------------------------------------------
    # V_LN_min < dip_threshold on any phase, AND not currently an outage.
    # Severity: deepest dip below nominal as a fraction (lower = worse).
    dip_mask = [
        not_outage[i]
        and any(v_min[p][i] < dip_threshold for p in range(3))
        for i in range(len(recs))
    ]
    for s, e in _group_runs(dip_mask, rules.gap_tolerance_secs):
        dipped = []
        deepest = float("inf")
        for p in range(3):
            phase_min = min(v_min[p][s:e + 1])
            if phase_min < dip_threshold:
                dipped.append(_PHASES[p])
                deepest = min(deepest, phase_min)
        emit("dip", s, e, severity=deepest / nominal_ln_v, phases=dipped)

    # --- swell --------------------------------------------------------------
    # V_LN_max > swell_threshold on any phase, while not in outage.
    # Severity: highest swell above nominal as a fraction (higher = worse).
    swell_mask = [
        not_outage[i]
        and any(v_max[p][i] > swell_threshold for p in range(3))
        for i in range(len(recs))
    ]
    for s, e in _group_runs(swell_mask, rules.gap_tolerance_secs):
        swelled = []
        highest = float("-inf")
        for p in range(3):
            phase_max = max(v_max[p][s:e + 1])
            if phase_max > swell_threshold:
                swelled.append(_PHASES[p])
                highest = max(highest, phase_max)
        emit("swell", s, e, severity=highest / nominal_ln_v, phases=swelled)

    # --- high_current -------------------------------------------------------
    # Per-phase: i_max[p] > mean(i_max[p]) + N*stdev(i_max[p]).
    # Severity: peak A across all flagged phases.
    for p in range(3):
        # Mask outage samples — current spikes during outages are restoration
        # transients, classified under "outage" already.
        valid = [v for i, v in enumerate(i_max[p]) if not_outage[i]]
        if len(valid) < 2:
            continue
        mu = mean(valid)
        sigma = pstdev(valid) if len(valid) >= 2 else 0.0
        if sigma == 0.0:
            continue
        threshold = mu + rules.high_current_sigma * sigma
        mask = [
            not_outage[i] and i_max[p][i] > threshold
            for i in range(len(recs))
        ]
        for s, e in _group_runs(mask, rules.gap_tolerance_secs):
            peak = max(i_max[p][s:e + 1])
            emit("high_current", s, e, severity=peak, phases=(_PHASES[p],))

    # --- freq_excursion -----------------------------------------------------
    # Severity: signed deviation in Hz (negative = under-frequency).
    freq_mask = [
        not_outage[i] and abs(freq[i] - rules.nominal_freq_hz) > rules.freq_excursion_hz
        for i in range(len(recs))
    ]
    for s, e in _group_runs(freq_mask, rules.gap_tolerance_secs):
        window = freq[s:e + 1]
        # Take the most-extreme deviation in the window
        worst = max(window, key=lambda f: abs(f - rules.nominal_freq_hz))
        emit("freq_excursion", s, e,
             severity=worst - rules.nominal_freq_hz, phases=())

    # --- imbalance_spike ----------------------------------------------------
    # NEMA: max-min across phase avg L-N voltages > N% of mean.
    imbal_mask = []
    imbal_pct = []
    for i in range(len(recs)):
        if not not_outage[i]:
            imbal_mask.append(False)
            imbal_pct.append(0.0)
            continue
        vs = [v_avg[p][i] for p in range(3)]
        m = sum(vs) / 3.0
        if m <= rules.outage_v_threshold:
            imbal_mask.append(False)
            imbal_pct.append(0.0)
            continue
        pct = (max(vs) - min(vs)) / m * 100.0
        imbal_pct.append(pct)
        imbal_mask.append(pct > rules.imbalance_pct_threshold)
    for s, e in _group_runs(imbal_mask, rules.gap_tolerance_secs):
        peak_pct = max(imbal_pct[s:e + 1])
        emit("imbalance_spike", s, e, severity=peak_pct, phases=_PHASES)

    # --- power_step ---------------------------------------------------------
    # 1-sec ΔP_total exceeding N% of session mean |P_total| (outage-masked).
    # Severity: signed step magnitude in W (positive = sudden import, negative = export).
    p_valid = [p for i, p in enumerate(p_total) if not_outage[i]]
    if len(p_valid) >= 2:
        baseline = mean(abs(p) for p in p_valid)
        step_threshold = baseline * rules.power_step_pct_of_mean
        if step_threshold > 0:
            for i in range(1, len(recs)):
                if not (not_outage[i] and not_outage[i - 1]):
                    continue
                delta = p_total[i] - p_total[i - 1]
                if abs(delta) > step_threshold:
                    emit("power_step", i, i, severity=delta, phases=_PHASES)

    # Re-sort by time (high_current and others were grouped per phase, may
    # interleave) and re-issue sequential ids so the final list is canonical.
    events.sort(key=lambda ev: (ev.t_start, ev.kind))
    return [
        Event(id=i, kind=ev.kind, t_start=ev.t_start, t_end=ev.t_end,
              severity=ev.severity, affected_phases=ev.affected_phases)
        for i, ev in enumerate(events)
    ]
