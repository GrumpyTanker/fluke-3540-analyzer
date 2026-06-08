"""Round-2 analysis features built on the ColumnStore.

Pure-ish functions (no argparse, minimal I/O helpers) so each is unit-testable:

- :func:`whole_session_stats` — per-channel count/min/percentiles/mean/stdev/max
  plus time spent above/below key thresholds. Streaming running mean/variance;
  percentiles from a bounded-error streaming sketch (stdlib only).
- :func:`classify_itic` — ITIC/CBEMA ride-through classification for a
  (magnitude %, duration ms) dip/outage point.
- :func:`time_of_day_profile` — diurnal avg/min/max envelope per time-of-day bin.
- :func:`bucket_label` / :func:`bucket_key` / :func:`parse_period` — time-bucket
  partitioning for --split-by.
- :func:`correlate_markers` — nearest-event lookup for --mark / --marks.

Stdlib only — no numpy.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from .store import ColumnStore


# --- Streaming percentile sketch --------------------------------------------

class _PercentileSketch:
    """Bounded-memory streaming quantiles via a fixed-width histogram.

    For power-quality channels the dynamic range is known and modest, so a
    linear histogram over [lo, hi] with a fixed bin count gives percentile
    error bounded by the bin width — adequate for reporting (p1/p5/median/
    p95/p99) and uses O(bins) memory regardless of N. Out-of-range samples are
    clamped into the end bins (and counted), so extremes never silently vanish.
    """

    __slots__ = ("lo", "hi", "nbins", "_bins", "_width", "n", "_min", "_max")

    def __init__(self, lo: float, hi: float, nbins: int = 4000) -> None:
        if hi <= lo:
            hi = lo + 1.0
        self.lo = lo
        self.hi = hi
        self.nbins = nbins
        self._bins = [0] * nbins
        self._width = (hi - lo) / nbins
        self.n = 0
        self._min = math.inf
        self._max = -math.inf

    def add(self, v: float) -> None:
        if v != v:  # NaN
            return
        self.n += 1
        if v < self._min:
            self._min = v
        if v > self._max:
            self._max = v
        idx = int((v - self.lo) / self._width)
        if idx < 0:
            idx = 0
        elif idx >= self.nbins:
            idx = self.nbins - 1
        self._bins[idx] += 1

    def quantile(self, q: float) -> float:
        if self.n == 0:
            return float("nan")
        target = q * self.n
        cum = 0
        for i, c in enumerate(self._bins):
            cum += c
            if cum >= target:
                # midpoint of the bin
                return self.lo + (i + 0.5) * self._width
        return self.hi


@dataclass
class _RunningMoments:
    n: int = 0
    mean: float = 0.0
    m2: float = 0.0  # sum of squared deviations (Welford)

    def add(self, v: float) -> None:
        if v != v:
            return
        self.n += 1
        delta = v - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (v - self.mean)

    @property
    def variance(self) -> float:
        return self.m2 / self.n if self.n > 0 else 0.0

    @property
    def stdev(self) -> float:
        return math.sqrt(self.variance)


# Channels reported in whole-session stats, with histogram ranges and units.
_STATS_CHANNELS: tuple[tuple[str, float, float, str], ...] = (
    ("V_LN_a_avg_V", 0.0, 400.0, "V"),
    ("V_LN_b_avg_V", 0.0, 400.0, "V"),
    ("V_LN_c_avg_V", 0.0, 400.0, "V"),
    ("I_a_avg_A", 0.0, 1000.0, "A"),
    ("I_b_avg_A", 0.0, 1000.0, "A"),
    ("I_c_avg_A", 0.0, 1000.0, "A"),
    ("freq_avg_Hz", 55.0, 65.0, "Hz"),
    ("P_total_avg_W", -2_000_000.0, 2_000_000.0, "W"),
    ("S_total_avg_VA", -2_000_000.0, 2_000_000.0, "VA"),
    ("Q_total_avg_VAR", -2_000_000.0, 2_000_000.0, "VAR"),
    ("PF_total_avg", -1.05, 1.05, ""),
)


def whole_session_stats(
    store: ColumnStore,
    undervoltage_v: float = 250.0,
    overcurrent_a: float = 800.0,
) -> dict:
    """Per-channel streaming statistics + threshold time accounting.

    Returns a dict keyed by channel name; each value carries count/min/p1/p5/
    median/mean/p95/p99/max/stdev. Also includes ``_thresholds`` with seconds
    and percentage spent under-voltage (any phase < ``undervoltage_v``,
    non-outage) and over-current (any phase > ``overcurrent_a``).
    """
    nrec = store.n
    out: dict = {}
    moments: dict[str, _RunningMoments] = {}
    sketches: dict[str, _PercentileSketch] = {}
    for name, lo, hi, _unit in _STATS_CHANNELS:
        moments[name] = _RunningMoments()
        sketches[name] = _PercentileSketch(lo, hi)

    cols = {name: store.col(name) for name, *_ in _STATS_CHANNELS}
    va = store.col("V_LN_a_avg_V"); vb = store.col("V_LN_b_avg_V"); vc = store.col("V_LN_c_avg_V")
    ia = store.col("I_a_avg_A"); ib = store.col("I_b_avg_A"); ic = store.col("I_c_avg_A")

    sec_undervoltage = 0
    sec_overcurrent = 0
    for i in range(nrec):
        for name in cols:
            v = cols[name][i]
            moments[name].add(v)
            sketches[name].add(v)
        not_outage = va[i] > 50.0 and vb[i] > 50.0 and vc[i] > 50.0
        if not_outage and (va[i] < undervoltage_v or vb[i] < undervoltage_v
                           or vc[i] < undervoltage_v):
            sec_undervoltage += 1
        if ia[i] > overcurrent_a or ib[i] > overcurrent_a or ic[i] > overcurrent_a:
            sec_overcurrent += 1

    for name, lo, hi, unit in _STATS_CHANNELS:
        m = moments[name]
        sk = sketches[name]
        if m.n == 0:
            continue
        out[name] = {
            "unit": unit,
            "count": m.n,
            "min": sk._min,
            "p1": sk.quantile(0.01),
            "p5": sk.quantile(0.05),
            "median": sk.quantile(0.50),
            "mean": m.mean,
            "p95": sk.quantile(0.95),
            "p99": sk.quantile(0.99),
            "max": sk._max,
            "stdev": m.stdev,
        }
    out["_thresholds"] = {
        "undervoltage_v": undervoltage_v,
        "sec_undervoltage": sec_undervoltage,
        "pct_undervoltage": (sec_undervoltage / nrec * 100) if nrec else 0.0,
        "overcurrent_a": overcurrent_a,
        "sec_overcurrent": sec_overcurrent,
        "pct_overcurrent": (sec_overcurrent / nrec * 100) if nrec else 0.0,
        "total_records": nrec,
    }
    return out


# --- CT-reversal auto-detection ---------------------------------------------
#
# A correctly-wired load draws real power: P_total > 0 essentially all the time.
# When an iFlex CT is clipped on backwards, P/Q/PF/energy come out negated, so a
# load reads as a persistent *generator* (P_total < 0). We flag a session when
# real power is negative for a high fraction of NON-OUTAGE time — outage samples
# (all phases collapsed) are excluded because P there is ~0/noise.
#
# Bimodal loads (e.g. a rectifier that toggles between a heavy ACTIVE draw and a
# light STANDBY state) defeat a naive count-based test: at low current the real-
# power sign is unreliable, so a session can read negative-P more than half the
# time while the *real* consumption — the high-current active state — is clearly
# positive (or clearly negative if the CTs really are backwards). The auto-detect
# therefore decides on the DOMINANT HIGH-CURRENT (active) state: it asks whether
# real power is negative when current is high. The whole-session count-based
# fields are still reported (for context / back-compat), but ``reversed`` and the
# operator notice key off the active state.

# Default per-phase current (A) above which a record counts as "active" load.
# Shared with the load-state classifier so the two views agree.
STANDBY_CURRENT_THRESHOLD_A = 50.0


def _mean_phase_current(ia, ib, ic, i: int) -> float:
    """Mean of the three per-phase avg currents at record ``i`` (NaN->skipped).

    Returns the average over the finite phases (0.0 if none are finite), so a
    single dropped phase doesn't drag the classifier toward standby.
    """
    s = 0.0
    k = 0
    for c in (ia[i], ib[i], ic[i]):
        if c == c and c not in (math.inf, -math.inf):  # finite
            s += c
            k += 1
    return (s / k) if k else 0.0


def detect_ct_reversal(
    store: ColumnStore,
    neg_fraction_threshold: float = 0.50,
    outage_v_threshold: float = 50.0,
    active_threshold_a: float = STANDBY_CURRENT_THRESHOLD_A,
) -> dict:
    """Detect a likely reversed-CT install, deciding on the active (high-I) state.

    Returns a dict:
        {
          "reversed": bool,            # decision (see basis below)
          "frac_negative": float,      # whole-session: fraction non-outage P<0
          "non_outage_records": int,
          "negative_records": int,
          "mean_p_w": float,           # mean P over finite non-outage records
          "threshold": float,          # neg_fraction_threshold
          # --- magnitude-weighted (active-state) decision ---
          "basis": "active" | "whole_session",
          "active_threshold_a": float,
          "active_records": int,       # non-outage records with mean I >= thresh
          "active_negative_records": int,
          "active_frac_negative": float,
          "active_mean_p_w": float,    # mean P over the active state
        }

    DECISION BASIS. When the session has a meaningful active (high-current)
    population, ``reversed`` is True iff real power is negative for at least
    ``neg_fraction_threshold`` of the ACTIVE records — i.e. the real consumption
    looks like export. This is robust for bimodal loads where the low-current
    standby sign is noise. If there is no active population (everything is below
    ``active_threshold_a``), the test falls back to the whole-session count-based
    fraction (``basis`` = "whole_session"). Non-finite P samples are skipped (the
    real meter occasionally emits NaN). ``reversed`` True means the data looks
    like a load wired with backwards CTs — re-run with ``--reverse-cts`` (or
    ``--auto-reverse-cts``) to correct it.
    """
    p = store.col("P_total_avg_W")
    va = store.col("V_LN_a_avg_V")
    vb = store.col("V_LN_b_avg_V")
    vc = store.col("V_LN_c_avg_V")
    ia = store.col("I_a_avg_A")
    ib = store.col("I_b_avg_A")
    ic = store.col("I_c_avg_A")
    n = store.n
    non_outage = 0
    negative = 0
    p_sum = 0.0
    p_count = 0
    active = 0
    active_negative = 0
    active_p_sum = 0.0
    active_p_count = 0
    for i in range(n):
        if va[i] > outage_v_threshold and vb[i] > outage_v_threshold and vc[i] > outage_v_threshold:
            non_outage += 1
            pv = p[i]
            finite = pv == pv and pv not in (math.inf, -math.inf)
            if finite:
                p_sum += pv
                p_count += 1
            if pv < 0:  # NaN < 0 is False, so non-finite never counts as negative
                negative += 1
            if _mean_phase_current(ia, ib, ic, i) >= active_threshold_a:
                active += 1
                if finite:
                    active_p_sum += pv
                    active_p_count += 1
                if pv < 0:
                    active_negative += 1
    frac = (negative / non_outage) if non_outage else 0.0
    mean_p = (p_sum / p_count) if p_count else 0.0
    active_frac = (active_negative / active) if active else 0.0
    active_mean_p = (active_p_sum / active_p_count) if active_p_count else 0.0

    if active > 0:
        basis = "active"
        reversed_ = active_frac >= neg_fraction_threshold
    else:
        basis = "whole_session"
        reversed_ = frac >= neg_fraction_threshold

    return {
        "reversed": reversed_,
        "frac_negative": frac,
        "non_outage_records": non_outage,
        "negative_records": negative,
        "mean_p_w": mean_p,
        "threshold": neg_fraction_threshold,
        "basis": basis,
        "active_threshold_a": active_threshold_a,
        "active_records": active,
        "active_negative_records": active_negative,
        "active_frac_negative": active_frac,
        "active_mean_p_w": active_mean_p,
    }


def ct_reversal_notice(result: dict) -> str:
    """A loud, explicit operator-facing notice for a flagged CT reversal.

    Keys the headline numbers off the ACTIVE (high-current) state when that is
    the decision basis, since the low-current standby sign is unreliable.
    """
    if result.get("basis") == "active":
        pct = result["active_frac_negative"] * 100.0
        mean_kw = result["active_mean_p_w"] / 1000.0
        basis_line = (
            f"  Real power is NEGATIVE for {pct:.1f}% of ACTIVE (high-current, "
            f"I >= {result['active_threshold_a']:.0f} A/phase) time "
            f"(active mean P = {mean_kw:.1f} kW). A load should draw positive "
            "real power when it is actually running."
        )
    else:
        pct = result["frac_negative"] * 100.0
        mean_kw = result["mean_p_w"] / 1000.0
        basis_line = (
            f"  Real power (P_total) is NEGATIVE for {pct:.1f}% of non-outage "
            f"time (mean P = {mean_kw:.1f} kW). A load should draw positive "
            "real power."
        )
    return (
        "  !!  CT REVERSAL DETECTED  !!\n"
        f"{basis_line}\n"
        "  This signature means one or more iFlex CT probes are clipped on "
        "backwards.\n"
        "  Re-run with --reverse-cts to negate P/Q/PF/energy, or "
        "--auto-reverse-cts to apply the correction automatically."
    )


# --- ITIC / CBEMA classification --------------------------------------------
#
# The ITIC (CBEMA) curve describes the voltage-deviation/duration envelope that
# IT equipment should ride through. We classify a (residual voltage %, duration)
# point into:
#   - "no_interruption": inside the envelope — equipment expected to keep running.
#   - "prohibited":      above the upper bound (overvoltage) — may damage.
#   - "no_damage":       below the lower bound (undervoltage/dropout) — equipment
#                        may shut down but should not be damaged.
# Magnitude here is the RESIDUAL voltage as a percentage of nominal (so a 70%
# dip means voltage fell to 70% of nominal). Duration is in seconds.
#
# Lower-bound (prohibited-below = "no_damage") breakpoints, residual % vs seconds
# (classic ITIC lower envelope). Below this line: dropout region (no_damage).
_ITIC_LOWER = [
    # (duration_secs, residual_pct_floor)
    (0.001, 0.0),    # < 1 ms: anything tolerated
    (0.003, 0.0),    # 1-3 ms transient
    (0.020, 70.0),   # 20 ms (~1 cycle region tightening)
    (0.500, 70.0),   # to 0.5 s: 70% floor
    (10.0, 80.0),    # 0.5-10 s: 80% floor
    (1e9, 90.0),     # steady state: 90% floor
]
# Upper-bound (prohibited-above). Above this line: overvoltage (prohibited).
_ITIC_UPPER = [
    (0.001, 500.0),
    (0.0001, 500.0),
    (0.003, 200.0),
    (0.5, 120.0),
    (10.0, 120.0),
    (1e9, 110.0),
]


def _interp_step(table: list[tuple[float, float]], duration: float) -> float:
    """Step/linear lookup: return the bound for the smallest table duration
    >= the point's duration (envelope is conservative)."""
    for dmax, val in table:
        if duration <= dmax:
            return val
    return table[-1][1]


def classify_itic(residual_pct: float, duration_secs: float) -> str:
    """Classify a voltage event against the ITIC (CBEMA) curve.

    Args:
        residual_pct: voltage during the event as % of nominal (e.g. 70 = a dip
            to 70%; 0 = full outage; 130 = a 30% swell).
        duration_secs: event duration in seconds.

    Returns "no_interruption", "prohibited", or "no_damage".
    """
    if duration_secs < 0:
        duration_secs = 0.0
    lower = _interp_step(_ITIC_LOWER, duration_secs)
    upper = _interp_step(_ITIC_UPPER, duration_secs)
    if residual_pct > upper:
        return "prohibited"
    if residual_pct < lower:
        return "no_damage"
    return "no_interruption"


# --- IEEE 519 THD compliance + IEEE 1159 / SARFI indices --------------------
#
# IEEE 519-2014 voltage-distortion limits for systems <= 1 kV are 8.0% THD and
# 5.0% any-single-harmonic; we report against the 5%/8% pair (assessed on the
# 95th-percentile per-phase V_THD, which is how 519 evaluates compliance).
# Current TDD limits depend on the short-circuit ratio Isc/IL which the meter
# does not record, so I_THD is reported as the 95th-percentile per phase
# (informational) without a hard pass/fail.

# (limit_name, threshold_pct) voltage limits.
IEEE519_V_THD_LIMIT_PCT = 8.0       # total voltage distortion limit (<=1 kV)
IEEE519_V_THD_PLANNING_PCT = 5.0    # planning level / single-harmonic guidance


def ieee519_compliance(store: ColumnStore) -> dict:
    """IEEE 519 voltage-THD compliance per phase (assessed at p95).

    Returns {"voltage": {phase: {p95, limit, planning, compliant}}, "current":
    {phase: {p95}}, "limit_v_thd": 8.0, ...}. A phase is ``compliant`` when its
    95th-percentile V_THD is at or under the 8% limit.
    """
    out: dict = {
        "limit_v_thd_pct": IEEE519_V_THD_LIMIT_PCT,
        "planning_v_thd_pct": IEEE519_V_THD_PLANNING_PCT,
        "voltage": {},
        "current": {},
    }
    all_compliant = True
    for ph in ("a", "b", "c"):
        vcol = store.col(f"V_THD_pct_{ph}_avg")
        sk = _PercentileSketch(0.0, 100.0, nbins=2000)
        for v in vcol:
            sk.add(v)
        p95 = sk.quantile(0.95) if sk.n else 0.0
        compliant = p95 <= IEEE519_V_THD_LIMIT_PCT
        all_compliant = all_compliant and compliant
        out["voltage"][ph] = {
            "p95": p95,
            "limit": IEEE519_V_THD_LIMIT_PCT,
            "planning": IEEE519_V_THD_PLANNING_PCT,
            "compliant": compliant,
            "exceeds_planning": p95 > IEEE519_V_THD_PLANNING_PCT,
        }
        icol = store.col(f"I_THD_pct_{ph}_avg")
        ski = _PercentileSketch(0.0, 200.0, nbins=2000)
        for v in icol:
            ski.add(v)
        out["current"][ph] = {"p95": ski.quantile(0.95) if ski.n else 0.0}
    out["all_voltage_compliant"] = all_compliant
    return out


# SARFI magnitude bins (IEEE 1159 / IEEE 1564): residual-voltage thresholds.
# SARFI-X counts events whose residual voltage dipped BELOW X% of nominal.
SARFI_THRESHOLDS = (90, 80, 70, 50, 10)


def sarfi_indices(events, nominal_ln_v: float) -> dict:
    """System Average RMS Frequency Index per threshold (SARFI-X).

    For a single monitoring point SARFI-X is simply the count of voltage events
    (dips + outages) whose residual voltage fell below X% of nominal. Returns
    {"SARFI-90": n, ..., "events_considered": m, "nominal_ln_v": v}.
    """
    counts = {f"SARFI-{x}": 0 for x in SARFI_THRESHOLDS}
    considered = 0
    for ev in events:
        if ev.kind == "dip":
            residual_pct = ev.severity * 100.0
        elif ev.kind == "outage":
            residual_pct = (ev.severity / nominal_ln_v * 100.0) if nominal_ln_v else 0.0
        else:
            continue
        considered += 1
        for x in SARFI_THRESHOLDS:
            if residual_pct < x:
                counts[f"SARFI-{x}"] += 1
    counts["events_considered"] = considered
    counts["nominal_ln_v"] = nominal_ln_v
    return counts


# --- Demand analysis (rolling peak demand) ----------------------------------
#
# Utilities bill demand on a sliding/block window average of real power (15 min
# is the most common interval). We compute a trailing rolling mean of
# P_total_avg_W over ``window_secs`` samples (1 sample = 1 s) and report the
# peak rolling demand and when it occurred, plus an optional decimated series.

def demand_analysis(
    store: ColumnStore,
    window_secs: int = 900,
    series_step_secs: int = 0,
) -> dict:
    """Rolling-window peak real-power demand.

    Args:
        window_secs: rolling window length (default 900 = 15 min).
        series_step_secs: if > 0, emit a decimated demand series sampled every
            this many seconds; if 0, no series is returned (just the peak).

    Returns {"window_secs", "peak_demand_w", "peak_demand_kw",
    "peak_window_end", "peak_window_start", "mean_demand_w", "n_windows",
    "series": [...]}. ``series`` entries are {"t": iso, "demand_w": float}.
    Non-finite P samples are treated as 0 for the running sum.
    """
    p = store.col("P_total_avg_W")
    n = store.n
    w = max(1, int(window_secs))
    out: dict = {
        "window_secs": w,
        "peak_demand_w": 0.0,
        "peak_demand_kw": 0.0,
        "peak_window_end": None,
        "peak_window_start": None,
        "mean_demand_w": 0.0,
        "n_windows": 0,
        "series": [],
    }
    if n == 0:
        return out
    # Trailing rolling sum.
    running = 0.0
    peak = -math.inf
    peak_i = -1
    demand_sum = 0.0
    demand_count = 0
    series: list[dict] = []
    step = max(0, int(series_step_secs))
    for i in range(n):
        pv = p[i]
        if pv != pv or pv in (math.inf, -math.inf):
            pv = 0.0
        running += pv
        if i >= w:
            old = p[i - w]
            if old != old or old in (math.inf, -math.inf):
                old = 0.0
            running -= old
        if i >= w - 1:  # a full window is available
            demand = running / w
            demand_sum += demand
            demand_count += 1
            if demand > peak:
                peak = demand
                peak_i = i
            if step and ((i - (w - 1)) % step == 0):
                series.append({
                    "t": store.end(i).isoformat(),
                    "demand_w": demand,
                })
    if peak_i >= 0:
        out["peak_demand_w"] = peak
        out["peak_demand_kw"] = peak / 1000.0
        out["peak_window_end"] = store.end(peak_i).isoformat()
        out["peak_window_start"] = store.start(peak_i - w + 1).isoformat()
        out["mean_demand_w"] = demand_sum / demand_count if demand_count else 0.0
        out["n_windows"] = demand_count
    out["series"] = series
    return out


# --- Time-bucket partitioning (--split-by) ----------------------------------

@dataclass(frozen=True)
class Period:
    """A parsed --split-by period."""
    kind: str            # "hour" | "day" | "week" | "duration"
    seconds: int         # bucket width in seconds (for duration / hour / day / week)


def parse_period(text: str) -> Period:
    """Parse a --split-by value: hour|day|week or a duration like 30m/6h/2d."""
    t = text.strip().lower()
    if t in ("hour", "hourly"):
        return Period("hour", 3600)
    if t in ("day", "daily"):
        return Period("day", 86400)
    if t in ("week", "weekly"):
        return Period("week", 7 * 86400)
    # duration form: <number><unit> where unit in s/m/h/d
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if t and t[-1] in units and t[:-1].isdigit():
        n = int(t[:-1])
        if n <= 0:
            raise ValueError(f"--split-by duration must be positive: {text!r}")
        return Period("duration", n * units[t[-1]])
    raise ValueError(
        f"Unrecognized --split-by period {text!r}. Use hour|day|week or a "
        "duration like 30m, 6h, 2d."
    )


def bucket_key(t: dt.datetime, period: Period) -> dt.datetime:
    """Return the clock-aligned bucket start datetime that contains ``t``.

    Sub-day durations align within each day (e.g. 6h → 00,06,12,18). day/week
    align to midnight (week to the containing midnight, day-stepped).
    """
    if period.kind == "week":
        midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
        # Align to a week grid anchored at the day's midnight, stepping by 7 days
        # from the Unix-epoch Monday is overkill; bucket per 7-day block from
        # the session's first midnight is handled by the caller. Here align to
        # the containing midnight (caller groups consecutive days).
        return midnight
    if period.kind == "day":
        return t.replace(hour=0, minute=0, second=0, microsecond=0)
    if period.kind == "hour":
        return t.replace(minute=0, second=0, microsecond=0)
    # duration: align within the day
    midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
    secs_into_day = int((t - midnight).total_seconds())
    aligned = (secs_into_day // period.seconds) * period.seconds
    return midnight + dt.timedelta(seconds=aligned)


def bucket_label(start: dt.datetime, period: Period) -> str:
    """Human/file-safe label for a bucket starting at ``start``."""
    if period.kind == "day" or period.kind == "week":
        return start.strftime("%Y-%m-%d")
    if period.kind == "hour":
        return start.strftime("%Y-%m-%dT%H")
    # duration: label with start..end-of-bucket hour-minute
    end = start + dt.timedelta(seconds=period.seconds)
    if period.seconds % 3600 == 0:
        return f"{start.strftime('%Y-%m-%dT%H')}-{end.strftime('%H')}"
    return f"{start.strftime('%Y-%m-%dT%H%M')}-{end.strftime('%H%M')}"


def assign_buckets(store: ColumnStore, period: Period) -> list[tuple[dt.datetime, int, int]]:
    """Partition store record indices into (bucket_start, lo, hi_exclusive) ranges.

    Records are in time order, so buckets are contiguous index ranges. For
    ``week`` the buckets are 7-day blocks anchored at the first record's
    midnight.
    """
    nrec = store.n
    if nrec == 0:
        return []
    buckets: list[tuple[dt.datetime, int, int]] = []
    if period.kind == "week":
        first_midnight = store.start(0).replace(
            hour=0, minute=0, second=0, microsecond=0)
        cur_key = None
        lo = 0
        for i in range(nrec):
            t = store.start(i)
            block = int((t - first_midnight).total_seconds()) // period.seconds
            key = first_midnight + dt.timedelta(seconds=block * period.seconds)
            if cur_key is None:
                cur_key = key
            elif key != cur_key:
                buckets.append((cur_key, lo, i))
                cur_key = key
                lo = i
        buckets.append((cur_key, lo, nrec))
        return buckets

    cur_key = None
    lo = 0
    for i in range(nrec):
        key = bucket_key(store.start(i), period)
        if cur_key is None:
            cur_key = key
        elif key != cur_key:
            buckets.append((cur_key, lo, i))
            cur_key = key
            lo = i
    buckets.append((cur_key, lo, nrec))
    return buckets


def slice_store(store: ColumnStore, lo: int, hi: int) -> ColumnStore:
    """Return a new ColumnStore over records [lo, hi) sharing the time_shift."""
    sub = ColumnStore(time_shift=store.time_shift)
    for name in store.columns:
        sub._cols[name].extend(store._cols[name][lo:hi])
    sub._start_ticks.extend(store._start_ticks[lo:hi])
    sub._end_ticks.extend(store._end_ticks[lo:hi])
    sub._n = hi - lo
    return sub


def gather_store(store: ColumnStore, indices: Sequence[int]) -> ColumnStore:
    """Like :func:`slice_store` but over an arbitrary (possibly non-contiguous)
    list of record indices.

    Used by shift aggregation: a "night" shift recurs daily, so its records
    are scattered through the store rather than forming one contiguous range.
    ``indices`` are taken in the given order (callers pass them ascending so
    timestamps stay monotonic, which keeps kWh/demand roll-ups well-defined).
    """
    sub = ColumnStore(time_shift=store.time_shift)
    cols = store.columns
    src = {name: store._cols[name] for name in cols}
    dst = {name: sub._cols[name] for name in cols}
    s_ticks = store._start_ticks
    e_ticks = store._end_ticks
    for i in indices:
        for name in cols:
            dst[name].append(src[name][i])
        sub._start_ticks.append(s_ticks[i])
        sub._end_ticks.append(e_ticks[i])
    sub._n = len(sub._start_ticks)
    return sub


# --- Generalized named shift/period splitting (--split-by shifts) ------------

UNASSIGNED_SHIFT = "unassigned"


def _parse_hhmm(text: str) -> int:
    """Parse 'HH:MM' (colon required) to minute-of-day in [0, 1440].

    '24:00' is accepted as end-of-day (1440). Raises ValueError otherwise.
    """
    s = text.strip()
    if ":" not in s:
        raise ValueError(f"shift time must be HH:MM (with a colon): {text!r}")
    hh, _, mm = s.partition(":")
    if not (hh.isdigit() and mm.isdigit()):
        raise ValueError(f"shift time must be numeric HH:MM: {text!r}")
    h = int(hh)
    m = int(mm)
    if m >= 60:
        raise ValueError(f"shift minutes out of range in {text!r}")
    total = h * 60 + m
    if total < 0 or total > 1440:
        raise ValueError(f"shift time out of range (00:00..24:00): {text!r}")
    return total


@dataclass(frozen=True)
class Shift:
    """One named shift window, in minutes-of-day (report-tz wall clock).

    ``end_min <= start_min`` means the window wraps past midnight, e.g.
    18:00-06:00 covers [18:00, 24:00) plus [00:00, 06:00).
    """
    name: str
    start_min: int
    end_min: int

    @property
    def wraps(self) -> bool:
        return self.end_min <= self.start_min

    def contains_minute(self, mod: int) -> bool:
        """Is minute-of-day ``mod`` inside this window? start inclusive, end
        exclusive; wrap-aware."""
        if not self.wraps:
            return self.start_min <= mod < self.end_min
        # Wrapping: [start, 1440) U [0, end)
        return mod >= self.start_min or mod < self.end_min

    def length_minutes(self) -> int:
        if self.wraps:
            return (1440 - self.start_min) + self.end_min
        return self.end_min - self.start_min

    @property
    def window_str(self) -> str:
        def fmt(m: int) -> str:
            return f"{(m // 60) % 24:02d}:{m % 60:02d}" if m != 1440 else "24:00"
        return f"{fmt(self.start_min)}-{fmt(self.end_min)}"


@dataclass(frozen=True)
class ShiftSet:
    """An ordered collection of named shift windows.

    Order matters: each record is assigned to the FIRST window it matches, so
    overlaps resolve deterministically.
    """
    shifts: tuple[Shift, ...]

    @classmethod
    def parse(cls, text: str) -> "ShiftSet":
        """Parse 'name=HH:MM-HH:MM,name=HH:MM-HH:MM,...'."""
        out: list[Shift] = []
        seen: set[str] = set()
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if not parts:
            raise ValueError(f"no shifts parsed from {text!r}")
        for part in parts:
            name, sep, window = part.partition("=")
            name = name.strip()
            if not sep or not name:
                raise ValueError(
                    f"shift must be name=HH:MM-HH:MM: {part!r}")
            a, dash, b = window.partition("-")
            if not dash:
                raise ValueError(
                    f"shift window must be HH:MM-HH:MM: {window!r}")
            start = _parse_hhmm(a)
            end = _parse_hhmm(b)
            if start == end:
                raise ValueError(
                    f"shift {name!r} has a zero-length window {window!r}")
            if name in seen:
                raise ValueError(f"duplicate shift name {name!r}")
            seen.add(name)
            out.append(Shift(name, start, end))
        return cls(tuple(out))

    @classmethod
    def from_spec(cls, spec: Sequence[dict]) -> "ShiftSet":
        """Build from a list of {name, start, end} dicts (the --shifts-file form)."""
        text = ",".join(
            f"{s['name']}={s['start']}-{s['end']}" for s in spec)
        return cls.parse(text)

    @classmethod
    def default(cls) -> "ShiftSet":
        return cls.parse("day=06:00-18:00,night=18:00-06:00")

    # --- validation --------------------------------------------------------
    def coverage_issues(self) -> list[str]:
        """Warn if the windows don't tile 24 h (gaps or overlaps).

        Walks minute-of-day 0..1439 counting how many windows cover each
        minute. Reports the count of uncovered minutes (gap) and doubly-covered
        minutes (overlap). Empty list = clean 24 h tiling.
        """
        cover = [0] * 1440
        for sh in self.shifts:
            for m in range(1440):
                if sh.contains_minute(m):
                    cover[m] += 1
        gap = sum(1 for c in cover if c == 0)
        overlap = sum(1 for c in cover if c > 1)
        issues: list[str] = []
        if gap:
            issues.append(
                f"{gap} minute(s)/day fall in NO shift window (gap); those "
                f"records go to '{UNASSIGNED_SHIFT}'.")
        if overlap:
            issues.append(
                f"{overlap} minute(s)/day are covered by MORE THAN ONE shift "
                "(overlap); the first matching window wins.")
        return issues


def _localize_minute(t: dt.datetime, tz) -> int:
    """Minute-of-day for ``t`` in the report timezone ``tz`` (UTC if None)."""
    if tz is not None:
        t = t.astimezone(tz)
    return t.hour * 60 + t.minute


def aggregate_shifts(store: ColumnStore, ss: ShiftSet, tz=None) -> dict[str, list[int]]:
    """Group record indices by shift NAME, evaluating windows in ``tz``.

    Returns {shift_name: [ascending indices]}. Each named shift present in
    ``ss`` always has a key (possibly empty). Records matching no window are
    collected under ``UNASSIGNED_SHIFT`` (only added when non-empty).
    """
    out: dict[str, list[int]] = {sh.name: [] for sh in ss.shifts}
    for i in range(store.n):
        mod = _localize_minute(store.start(i), tz)
        for sh in ss.shifts:
            if sh.contains_minute(mod):
                out[sh.name].append(i)
                break
        else:
            out.setdefault(UNASSIGNED_SHIFT, []).append(i)
    return out


def shift_occurrences(store: ColumnStore, ss: ShiftSet, tz=None
                      ) -> list[tuple[str, str, int, int]]:
    """Partition records into contiguous per-OCCURRENCE shift buckets.

    Returns a time-ordered list of (label, shift_name, lo, hi_exclusive). A
    new occurrence starts whenever the matched shift name changes between
    consecutive records. A wrap-past-midnight window stays ONE occurrence
    (the name does not change), labeled by the occurrence's START date in the
    report timezone, e.g. ``"night 2026-05-29"``.
    """
    n = store.n
    if n == 0:
        return []

    def name_at(i: int) -> str:
        mod = _localize_minute(store.start(i), tz)
        for sh in ss.shifts:
            if sh.contains_minute(mod):
                return sh.name
        return UNASSIGNED_SHIFT

    def start_date(i: int) -> str:
        t = store.start(i)
        if tz is not None:
            t = t.astimezone(tz)
        return t.strftime("%Y-%m-%d")

    out: list[tuple[str, str, int, int]] = []
    cur_name = name_at(0)
    lo = 0
    for i in range(1, n):
        nm = name_at(i)
        if nm != cur_name:
            out.append((f"{cur_name} {start_date(lo)}", cur_name, lo, i))
            cur_name = nm
            lo = i
    out.append((f"{cur_name} {start_date(lo)}", cur_name, lo, n))
    return out


def shift_comparison_rows(store: ColumnStore, ss: ShiftSet, events: Sequence,
                          tz=None, nominal_ln_v: float = 277.0,
                          demand_window: int = 15,
                          standby_threshold_a: float = STANDBY_CURRENT_THRESHOLD_A,
                          ) -> list[dict]:
    """The headline per-shift-name aggregate comparison.

    One row per named shift (plus ``unassigned`` if any records land there),
    aggregating ALL records of that shift across the whole session. ``events``
    are filed to a shift by the shift their ``t_start`` falls in.

    ``demand_window`` is in MINUTES (peak rolling demand within the shift's
    records). Per-row energy/avg/percentiles are memory-bounded. Each row also
    carries its active-state load (``active_duty_pct``, ``active_kWh``,
    ``active_PF_avg``) using ``standby_threshold_a`` as the active/standby
    current cut.
    """
    by_name = aggregate_shifts(store, ss, tz=tz)
    win_by_name = {sh.name: sh for sh in ss.shifts}

    # File events to a shift by their start time's window.
    ev_by_name: dict[str, list] = {nm: [] for nm in by_name}
    for e in events:
        mod = _localize_minute(e.t_start, tz)
        placed = False
        for sh in ss.shifts:
            if sh.contains_minute(mod):
                ev_by_name.setdefault(sh.name, []).append(e)
                placed = True
                break
        if not placed:
            ev_by_name.setdefault(UNASSIGNED_SHIFT, []).append(e)

    rows: list[dict] = []
    for name, idxs in by_name.items():
        sub = gather_store(store, idxs)
        sh = win_by_name.get(name)
        window = sh.window_str if sh is not None else "—"
        bucket_events = ev_by_name.get(name, [])
        rows.append(_shift_row(name, window, sub, bucket_events,
                               nominal_ln_v, demand_window,
                               standby_threshold_a))
    return rows


def _shift_row(name: str, window: str, sub: ColumnStore, bucket_events,
               nominal_ln_v: float, demand_window: int,
               standby_threshold_a: float = STANDBY_CURRENT_THRESHOLD_A) -> dict:
    """Aggregate one shift's records into a comparison row."""
    nrec = sub.n
    p = sub.col("P_total_avg_W")
    pf = sub.col("PF_total_avg")
    va = sub.col("V_LN_a_avg_V"); vb = sub.col("V_LN_b_avg_V"); vc = sub.col("V_LN_c_avg_V")
    ia = sub.col("I_a_avg_A"); ib = sub.col("I_b_avg_A"); ic = sub.col("I_c_avg_A")
    vth_a = sub.col("V_THD_pct_a_avg")
    vth_b = sub.col("V_THD_pct_b_avg")
    vth_c = sub.col("V_THD_pct_c_avg")

    p_mom = _RunningMoments()
    p_min = math.inf; p_max = -math.inf
    pf_mom = _RunningMoments()
    v_mom = _RunningMoments()
    v_sketch = _PercentileSketch(0.0, 400.0)
    vthd_sketch = _PercentileSketch(0.0, 50.0)
    # Active-state (high-current) sub-aggregates for the shift's own load split.
    act_p_mom = _RunningMoments()
    act_pf_mom = _RunningMoments()
    act_records = 0
    for i in range(nrec):
        pv = p[i]
        is_active = _mean_phase_current(ia, ib, ic, i) >= standby_threshold_a
        if is_active:
            act_records += 1
        if math.isfinite(pv):
            p_mom.add(pv)
            p_min = min(p_min, pv); p_max = max(p_max, pv)
            if is_active:
                act_p_mom.add(pv)
        pfi = pf[i]
        if math.isfinite(pfi):
            pf_mom.add(pfi)
            if is_active:
                act_pf_mom.add(pfi)
        for vv in (va[i], vb[i], vc[i]):
            if math.isfinite(vv) and vv > 50.0:  # ignore outage zeros
                v_mom.add(vv); v_sketch.add(vv)
        for tv in (vth_a[i], vth_b[i], vth_c[i]):
            if math.isfinite(tv):
                vthd_sketch.add(tv)

    p_mean = p_mom.mean if p_mom.n else 0.0
    hours = p_mom.n / 3600.0  # 1 record == 1 s
    kwh = p_mean / 1000.0 * hours

    act_p_mean = act_p_mom.mean if act_p_mom.n else 0.0
    act_hours = act_p_mom.n / 3600.0
    act_kwh = act_p_mean / 1000.0 * act_hours
    act_duty_pct = (act_records / nrec * 100.0) if nrec else 0.0

    # Peak rolling demand within the shift's gathered records.
    demand = demand_analysis(sub, window_secs=max(1, demand_window * 60))

    n_out = sum(1 for e in bucket_events if e.kind == "outage")
    n_dip = sum(1 for e in bucket_events if e.kind == "dip")
    n_swell = sum(1 for e in bucket_events if e.kind == "swell")
    outage_minutes = sum(
        (e.t_end - e.t_start).total_seconds() for e in bucket_events
        if e.kind == "outage") / 60.0

    def q(sketch, p_):
        v = sketch.quantile(p_)
        return 0.0 if (v != v) else v

    return {
        "shift": name,
        "window": window,
        "records": nrec,
        "hours": hours,
        "kWh": kwh,
        "P_total_avg_W": p_mean,
        "P_total_min_W": (p_min if p_min != math.inf else 0.0),
        "P_total_max_W": (p_max if p_max != -math.inf else 0.0),
        "peak_demand_kW": demand["peak_demand_kw"],
        "peak_demand_window_secs": demand["window_secs"],
        "PF_avg": (pf_mom.mean if pf_mom.n else 0.0),
        "V_LN_avg_V": (v_mom.mean if v_mom.n else 0.0),
        "V_LN_p5_V": q(v_sketch, 0.05),
        "V_LN_p95_V": q(v_sketch, 0.95),
        "V_THD_p95_pct": q(vthd_sketch, 0.95),
        "n_outages": n_out,
        "n_dips": n_dip,
        "n_swells": n_swell,
        "outage_minutes": outage_minutes,
        # Active-state load (current-gated) for this shift.
        "active_records": act_records,
        "active_duty_pct": act_duty_pct,
        "active_kWh": act_kwh,
        "active_PF_avg": (act_pf_mom.mean if act_pf_mom.n else 0.0),
    }


# --- Load-state split (active vs standby, current-gated) --------------------
#
# Real bimodal loads (e.g. a coating rectifier) alternate between a heavy ACTIVE
# draw and a light STANDBY state. The two states have very different — and not
# uniformly trustworthy — power signatures, so blending them into one session
# mean buries the real consumption and produces a meaningless mean PF.
#
# We classify each record by mean per-phase CURRENT (not power, because the
# power SIGN at low current is exactly the thing in question): a record is
# ``active`` when (I_a_avg + I_b_avg + I_c_avg)/3 >= the threshold (default
# 50 A), else ``standby``. The two states are then aggregated and reported
# separately, and three energy figures are surfaced (see ``session_energy``).

LOAD_STATES = ("active", "standby")


def classify_load_states(
    store: ColumnStore,
    threshold_a: float = STANDBY_CURRENT_THRESHOLD_A,
) -> dict[str, list[int]]:
    """Partition record indices into ``active`` / ``standby`` by mean current.

    Returns ``{"active": [ascending idx], "standby": [ascending idx]}``. A
    record is active when its mean per-phase avg current is >= ``threshold_a``.
    Records with no finite phase current read 0 A and fall to standby.
    """
    ia = store.col("I_a_avg_A")
    ib = store.col("I_b_avg_A")
    ic = store.col("I_c_avg_A")
    active: list[int] = []
    standby: list[int] = []
    for i in range(store.n):
        if _mean_phase_current(ia, ib, ic, i) >= threshold_a:
            active.append(i)
        else:
            standby.append(i)
    return {"active": active, "standby": standby}


def _load_state_row(name: str, sub: ColumnStore, total_records: int) -> dict:
    """Aggregate one load state's gathered records into a comparison row."""
    nrec = sub.n
    p = sub.col("P_total_avg_W")
    pf = sub.col("PF_total_avg")
    s = sub.col("S_total_avg_VA")
    va = sub.col("V_LN_a_avg_V"); vb = sub.col("V_LN_b_avg_V"); vc = sub.col("V_LN_c_avg_V")
    ia = sub.col("I_a_avg_A"); ib = sub.col("I_b_avg_A"); ic = sub.col("I_c_avg_A")
    vth_a = sub.col("V_THD_pct_a_avg")
    vth_b = sub.col("V_THD_pct_b_avg")
    vth_c = sub.col("V_THD_pct_c_avg")

    p_mom = _RunningMoments()
    p_min = math.inf; p_max = -math.inf
    pf_mom = _RunningMoments()
    s_mom = _RunningMoments()
    i_mom = _RunningMoments()
    v_mom = _RunningMoments()
    vthd_sketch = _PercentileSketch(0.0, 50.0)
    for i in range(nrec):
        pv = p[i]
        if math.isfinite(pv):
            p_mom.add(pv)
            p_min = min(p_min, pv); p_max = max(p_max, pv)
        pfi = pf[i]
        if math.isfinite(pfi):
            pf_mom.add(pfi)
        sv = s[i]
        if math.isfinite(sv):
            s_mom.add(sv)
        i_mom.add(_mean_phase_current(ia, ib, ic, i))
        for vv in (va[i], vb[i], vc[i]):
            if math.isfinite(vv) and vv > 50.0:  # ignore outage zeros
                v_mom.add(vv)
        for tv in (vth_a[i], vth_b[i], vth_c[i]):
            if math.isfinite(tv):
                vthd_sketch.add(tv)

    p_mean = p_mom.mean if p_mom.n else 0.0
    hours = p_mom.n / 3600.0  # 1 record == 1 s
    kwh = p_mean / 1000.0 * hours

    def q(sketch, p_):
        v = sketch.quantile(p_)
        return 0.0 if (v != v) else v

    return {
        "state": name,
        "records": nrec,
        "hours": hours,
        "duty_pct": (nrec / total_records * 100.0) if total_records else 0.0,
        "kWh": kwh,
        "P_avg_kW": p_mean / 1000.0,
        "P_min_kW": (p_min / 1000.0 if p_min != math.inf else 0.0),
        "P_max_kW": (p_max / 1000.0 if p_max != -math.inf else 0.0),
        "I_avg_A": (i_mom.mean if i_mom.n else 0.0),
        "S_avg_kVA": (s_mom.mean / 1000.0 if s_mom.n else 0.0),
        "PF_avg": (pf_mom.mean if pf_mom.n else 0.0),
        "V_LN_avg_V": (v_mom.mean if v_mom.n else 0.0),
        "V_THD_p95_pct": q(vthd_sketch, 0.95),
    }


def load_state_rows(
    store: ColumnStore,
    threshold_a: float = STANDBY_CURRENT_THRESHOLD_A,
) -> list[dict]:
    """Per-load-state comparison rows (one each for ``active`` then ``standby``).

    Each row carries records/hours/duty_pct/kWh, P avg/min/max (kW), I_avg (A),
    S_avg (kVA), PF_avg, V_LN_avg (V), and V_THD_p95 (%). Rows are always in the
    fixed order (active, standby) so downstream tables are stable.
    """
    groups = classify_load_states(store, threshold_a)
    total = store.n
    rows: list[dict] = []
    for name in LOAD_STATES:
        sub = gather_store(store, groups[name])
        rows.append(_load_state_row(name, sub, total))
    return rows


def session_energy(
    store: ColumnStore,
    threshold_a: float = STANDBY_CURRENT_THRESHOLD_A,
) -> dict:
    """Three explicitly-labeled session energy figures (kWh) + the caveat note.

    Returns::

        {
          "energy_as_measured_kWh": float,   # signed sum — current behavior
          "energy_active_kWh": float,        # active (high-I) records only
          "energy_net_clip_standby_kWh": float,  # standby real power clipped >=0
          "standby_threshold_a": float,
          "note": str,
        }

    All three use the same kWh convention as the rest of the tool: per record
    (1 s) energy = P_total_avg_W / 1000 / 3600, summed. Non-finite P samples are
    skipped. ``energy_as_measured_kWh`` is unchanged from the historic signed
    sum; the active / clip figures correct for the unreliable low-current
    standby sign and are the defensible consumption.
    """
    p = store.col("P_total_avg_W")
    ia = store.col("I_a_avg_A")
    ib = store.col("I_b_avg_A")
    ic = store.col("I_c_avg_A")
    per_kwh = 1.0 / 1000.0 / 3600.0  # W * 1 s -> kWh

    as_measured = 0.0
    active = 0.0
    net_clip = 0.0
    for i in range(store.n):
        pv = p[i]
        if not (pv == pv and pv not in (math.inf, -math.inf)):  # skip non-finite
            continue
        e = pv * per_kwh
        as_measured += e
        if _mean_phase_current(ia, ib, ic, i) >= threshold_a:
            active += e
            net_clip += e  # active records pass through unchanged
        else:
            # standby: clip real power to >= 0 (a rectifier in standby draws
            # small positive losses, never exports).
            if pv > 0:
                net_clip += e
    return {
        "energy_as_measured_kWh": as_measured,
        "energy_active_kWh": active,
        "energy_net_clip_standby_kWh": net_clip,
        "standby_threshold_a": threshold_a,
        "note": (
            "Standby real-power SIGN is unreliable at low current, so the "
            "as-measured signed sum can understate consumption. energy_active "
            "(active records only) and energy_net_clip_standby (standby real "
            "power clipped to >=0) are the defensible consumption figures."
        ),
    }


def active_state_pf(
    rows: Sequence[dict],
) -> float | None:
    """Pull the active-state mean PF out of :func:`load_state_rows` output.

    Returns the active row's ``PF_avg`` (the meaningful headline PF for a
    bimodal load), or ``None`` if there is no active row.
    """
    for r in rows:
        if r.get("state") == "active":
            return r.get("PF_avg")
    return None


# --- Event markers / correlation (--mark / --marks) -------------------------

@dataclass(frozen=True)
class Marker:
    time: dt.datetime
    label: str


def correlate_markers(markers: Sequence[Marker], events) -> list[dict]:
    """For each marker, find the nearest detected event and the offset.

    Returns a list of dicts (JSON-ready): marker time/label, nearest event id/
    kind/time, and signed offset in seconds (marker - event). Negative offset
    means the event preceded the marker.
    """
    out: list[dict] = []
    for m in markers:
        nearest = None
        best = None
        for ev in events:
            off = (m.time - ev.t_start).total_seconds()
            if best is None or abs(off) < abs(best):
                best = off
                nearest = ev
        entry = {
            "marker_time": m.time.isoformat(),
            "label": m.label,
            "nearest_event": None,
        }
        if nearest is not None:
            entry["nearest_event"] = {
                "id": nearest.id,
                "kind": nearest.kind,
                "t_start": nearest.t_start.isoformat(),
                "offset_secs": best,  # marker - event_start
            }
        out.append(entry)
    return out


# --- Time-of-day (diurnal) profile ------------------------------------------

def event_itic(ev, nominal_ln_v: float) -> dict:
    """Compute ITIC inputs + classification for a dip/outage event.

    Returns {residual_pct, duration_secs, itic_class} or {} for non-voltage
    events. For dips, ``severity`` is the residual fraction of nominal; for
    outages it is the deepest min L-N voltage (so residual = sev/nominal*100).
    """
    duration = (ev.t_end - ev.t_start).total_seconds()
    if ev.kind == "dip":
        residual_pct = ev.severity * 100.0
    elif ev.kind == "outage":
        residual_pct = (ev.severity / nominal_ln_v * 100.0) if nominal_ln_v else 0.0
    elif ev.kind == "swell":
        residual_pct = ev.severity * 100.0
    else:
        return {}
    cls = classify_itic(residual_pct, duration)
    return {
        "residual_pct": residual_pct,
        "duration_secs": duration,
        "itic_class": cls,
    }


def bucket_summary_row(label: str, sub: ColumnStore,
                       bucket_events) -> dict:
    """One per-bucket summary row: V/I extremes, kWh, event counts, PF, peak kW."""
    nrec = sub.n
    va = sub.col("V_LN_a_avg_V"); vb = sub.col("V_LN_b_avg_V"); vc = sub.col("V_LN_c_avg_V")
    ia = sub.col("I_a_avg_A"); ib = sub.col("I_b_avg_A"); ic = sub.col("I_c_avg_A")
    p = sub.col("P_total_avg_W"); pf = sub.col("PF_total_avg")

    vmin = math.inf; vmax = -math.inf; vsum = 0.0; vcount = 0
    imax = 0.0
    peak_kw = -math.inf
    pf_worst = 1.0
    p_sum = 0.0
    p_count = 0
    for i in range(nrec):
        for vv in (va[i], vb[i], vc[i]):
            if math.isfinite(vv) and vv > 50.0:  # ignore outage zeros for min/avg
                vmin = min(vmin, vv); vsum += vv; vcount += 1
            if math.isfinite(vv):
                vmax = max(vmax, vv)
        for cc in (ia[i], ib[i], ic[i]):
            if math.isfinite(cc):
                imax = max(imax, cc)
        pv = p[i]
        if math.isfinite(pv):
            peak_kw = max(peak_kw, pv / 1000.0)
            p_sum += pv
            p_count += 1
        if (math.isfinite(va[i]) and va[i] > 50.0 and math.isfinite(vb[i])
                and vb[i] > 50.0 and math.isfinite(vc[i]) and vc[i] > 50.0):
            if math.isfinite(pf[i]) and abs(pf[i]) < abs(pf_worst):
                pf_worst = pf[i]
    # kWh via mean power * hours (1 record = 1 s); non-finite samples skipped.
    p_mean = p_sum / p_count if p_count else 0.0
    kwh = p_mean / 1000.0 * (p_count / 3600.0)

    n_out = sum(1 for e in bucket_events if e.kind == "outage")
    n_dip = sum(1 for e in bucket_events if e.kind == "dip")
    n_swell = sum(1 for e in bucket_events if e.kind == "swell")
    return {
        "bucket": label,
        "records": nrec,
        "V_min_V": (vmin if vmin != math.inf else 0.0),
        "V_avg_V": (vsum / vcount if vcount else 0.0),
        "V_max_V": (vmax if vmax != -math.inf else 0.0),
        "I_max_A": imax,
        "kWh": kwh,
        "n_outages": n_out,
        "n_dips": n_dip,
        "n_swells": n_swell,
        "worst_PF": pf_worst,
        "peak_kW": (peak_kw if peak_kw != -math.inf else 0.0),
    }


def parse_tod_window(text: str) -> tuple[int, int]:
    """Parse 'HH:MM-HH:MM' into (start_min_of_day, end_min_of_day).

    '00:00-24:00' (or '00:00-00:00') means the full day. Returns minutes since
    midnight; end may be 1440 for end-of-day.
    """
    a, _, b = text.partition("-")
    def to_min(s: str) -> int:
        s = s.strip()
        hh, _, mm = s.partition(":")
        h = int(hh); m = int(mm) if mm else 0
        return h * 60 + m
    start = to_min(a)
    end = to_min(b)
    if end == 0:
        end = 1440
    return start, end


def time_of_day_profile(
    store: ColumnStore,
    window: tuple[int, int] = (0, 1440),
    bin_minutes: int = 1,
) -> list[dict]:
    """Bin P/V/I by time-of-day across all days; return avg/min/max envelope.

    Each output row: bin_start (HH:MM), n samples, n_days contributing, and
    avg/min/max for P (kW), V_LN_a (V), I_a (A). Only samples whose minute-of-
    day falls in ``window`` are included.
    """
    start_min, end_min = window
    nbins = (1440 + bin_minutes - 1) // bin_minutes
    p = store.col("P_total_avg_W")
    va = store.col("V_LN_a_avg_V")
    ia = store.col("I_a_avg_A")

    agg = [None] * nbins  # each: dict of running aggregates
    days_per_bin = [set() for _ in range(nbins)]

    for i in range(store.n):
        t = store.start(i)
        mod = t.hour * 60 + t.minute
        if not (start_min <= mod < end_min):
            continue
        b = mod // bin_minutes
        if b >= nbins:
            b = nbins - 1
        pv = p[i] / 1000.0  # kW
        if agg[b] is None:
            agg[b] = {
                "n": 0,
                "p_sum": 0.0, "p_min": math.inf, "p_max": -math.inf,
                "v_sum": 0.0, "v_min": math.inf, "v_max": -math.inf,
                "i_sum": 0.0, "i_min": math.inf, "i_max": -math.inf,
            }
        a = agg[b]
        a["n"] += 1
        a["p_sum"] += pv; a["p_min"] = min(a["p_min"], pv); a["p_max"] = max(a["p_max"], pv)
        a["v_sum"] += va[i]; a["v_min"] = min(a["v_min"], va[i]); a["v_max"] = max(a["v_max"], va[i])
        a["i_sum"] += ia[i]; a["i_min"] = min(a["i_min"], ia[i]); a["i_max"] = max(a["i_max"], ia[i])
        days_per_bin[b].add(t.date())

    rows: list[dict] = []
    for b in range(nbins):
        a = agg[b]
        if a is None or a["n"] == 0:
            continue
        bin_start_min = b * bin_minutes
        rows.append({
            "bin": f"{bin_start_min // 60:02d}:{bin_start_min % 60:02d}",
            "n": a["n"],
            "n_days": len(days_per_bin[b]),
            "p_avg_kW": a["p_sum"] / a["n"],
            "p_min_kW": a["p_min"],
            "p_max_kW": a["p_max"],
            "v_avg_V": a["v_sum"] / a["n"],
            "v_min_V": a["v_min"],
            "v_max_V": a["v_max"],
            "i_avg_A": a["i_sum"] / a["n"],
            "i_min_A": a["i_min"],
            "i_max_A": a["i_max"],
        })
    return rows
