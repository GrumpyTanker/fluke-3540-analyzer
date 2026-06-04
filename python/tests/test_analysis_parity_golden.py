"""Emit golden analysis outputs for the JS parity test (Feature B).

This test builds a deterministic session, runs the Python analysis functions,
and writes the results to python/tests/fixtures/analysis_golden.json. The JS
side (web/tests/analysis_parity.test.js) loads the same JSON and asserts its
own port produces identical numbers within float tolerance.

Keeping the generator in pytest (rather than a standalone script) means the
golden file is regenerated whenever the suite runs, so it can never drift from
the Python implementation.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from fluke_3540.analysis import (
    classify_itic, demand_analysis, detect_ct_reversal, event_itic,
    ieee519_compliance, sarfi_indices, time_of_day_profile, whole_session_stats,
)
from fluke_3540.events import Event
from fluke_3540.insights import Finding
from fluke_3540.narrative import build_narrative
from fluke_3540.store import ColumnStore

from conftest import make_records, plant_window


GOLDEN_PATH = Path(__file__).parent / "fixtures" / "analysis_golden.json"


def _build_session() -> ColumnStore:
    """A deterministic multi-shape session the JS test recreates exactly.

    - 600 records (10 minutes) starting 2024-01-13 22:00:00 UTC
    - P_total ramps so mean/percentiles are non-trivial
    - V_LN_a wobbles for a real stdev
    - one undervoltage window and one overcurrent window for thresholds
    - one dip window for the time-of-day / ITIC checks
    """
    overrides: dict = {}
    for i in range(600):
        overrides.setdefault(i, {})
        overrides[i]["P_total_avg_W"] = 50_000.0 + (i % 50) * 1000.0
        overrides[i]["V_LN_a_avg_V"] = 277.0 + (i % 7) - 3.0
        overrides[i]["I_a_avg_A"] = 100.0 + (i % 11)
        overrides[i]["PF_total_avg"] = 0.90 + (i % 5) * 0.01
        overrides[i]["freq_avg_Hz"] = 60.0 + ((i % 3) - 1) * 0.01
        overrides[i]["V_THD_pct_a_avg"] = 3.0 + (i % 13) * 0.5   # spans planning/limit
        overrides[i]["V_THD_pct_b_avg"] = 2.0 + (i % 5) * 0.2
        overrides[i]["V_THD_pct_c_avg"] = 4.5 + (i % 3) * 0.1
        overrides[i]["I_THD_pct_a_avg"] = 8.0 + (i % 7)
    plant_window(overrides, 100, 119, {"V_LN_a_avg_V": 240.0})   # undervoltage 20 s
    plant_window(overrides, 200, 204, {"I_c_avg_A": 850.0})      # overcurrent 5 s
    recs = make_records(600, overrides=overrides)
    return ColumnStore.from_records(recs)


def test_emit_analysis_golden():
    store = _build_session()
    stats = whole_session_stats(store)
    tod = time_of_day_profile(store, window=(0, 1440), bin_minutes=1)

    # A handful of ITIC classifications spanning each region.
    itic_points = [
        [70.0, 0.1],
        [60.0, 1.0],
        [130.0, 0.4],
        [95.0, 5.0],
        [0.0, 18.0],
    ]
    itic = [classify_itic(p, d) for p, d in itic_points]

    # event_itic on a representative dip + outage + swell.
    base = dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)
    sample_events = [
        Event(0, "dip", base, base + dt.timedelta(seconds=2), 0.72, ("a",)),
        Event(1, "outage", base, base + dt.timedelta(seconds=120), 0.0, ("a", "b", "c")),
        Event(2, "swell", base, base + dt.timedelta(seconds=1), 1.14, ("b",)),
    ]
    event_itic_out = [event_itic(e, 277.0) for e in sample_events]

    # CT-reversal detection on a mixed session: 70% negative-P, 30% positive.
    ct_overrides: dict = {}
    plant_window(ct_overrides, 0, 69, {"P_total_avg_W": -30_000.0})
    plant_window(ct_overrides, 70, 99, {"P_total_avg_W": 30_000.0})
    ct_store = ColumnStore.from_records(make_records(100, overrides=ct_overrides))
    ct = detect_ct_reversal(ct_store)

    # Narrative golden — fixed events/findings/stats/ct so the JS port can match.
    narr_base = dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)
    narr_events = [
        Event(0, "dip", narr_base + dt.timedelta(seconds=90),
              narr_base + dt.timedelta(seconds=92), 0.72, ("c",)),
        Event(1, "outage", narr_base + dt.timedelta(seconds=100),
              narr_base + dt.timedelta(seconds=1210), 0.0, ("a", "b", "c")),
    ]
    narr_findings = [
        Finding(0, "pf_drift", "alert",
                "Power factor below 0.85 for 99.8% of non-outage time",
                "", (), ()),
    ]
    narr_stats = {
        "PF_total_avg": {"mean": 0.81, "p5": 0.70, "p95": 0.95},
        "_thresholds": {"total_records": 590000},
    }
    narr_ct = {"reversed": True, "frac_negative": 0.52}
    narrative = build_narrative(
        narr_events, narr_findings, narr_stats, narr_ct,
        config={"asset_name": "MAC03"}, total_records=590000,
        duration_secs=604800.0,
    )

    # IEEE 519 on the main session; SARFI on the sample events.
    ieee519 = ieee519_compliance(store)
    sarfi = sarfi_indices(sample_events, 277.0)

    # Demand on a deterministic ramp store (P = i*100 W over 600 s), 120 s window.
    ramp_over = {i: {"P_total_avg_W": float(i) * 100.0} for i in range(600)}
    ramp_store = ColumnStore.from_records(make_records(600, overrides=ramp_over))
    demand = demand_analysis(ramp_store, window_secs=120, series_step_secs=120)

    golden = {
        "stats": stats,
        "tod_rows": tod,
        "itic_points": itic_points,
        "itic": itic,
        "event_itic": event_itic_out,
        "ct_reversal": ct,
        "narrative": narrative,
        "ieee519": ieee519,
        "sarfi": sarfi,
        "demand": demand,
    }

    # Timezone formatting golden (Feature H): a fixed UTC instant rendered in
    # UTC and America/Chicago. Stored as epoch ms so the JS test uses the same
    # instant regardless of how it parses ISO.
    from fluke_3540.tzutil import format_local_utc, resolve_tz
    tz_instant = dt.datetime(2024, 1, 13, 15, 0, 0, tzinfo=dt.timezone.utc)
    golden["timezone"] = {
        "epoch_ms": int(tz_instant.timestamp() * 1000),
        "utc": format_local_utc(tz_instant, None),
        "chicago": format_local_utc(tz_instant, resolve_tz("America/Chicago")),
    }
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(json.dumps(golden, indent=2), encoding="utf-8")

    # Sanity assertions so this test fails loudly if analysis regresses.
    assert stats["_thresholds"]["sec_undervoltage"] == 20
    assert stats["_thresholds"]["sec_overcurrent"] == 5
    assert stats["P_total_avg_W"]["count"] == 600
    assert itic[0] == "no_interruption"
    assert len(tod) > 0
    assert ct["reversed"] is True
    assert abs(ct["frac_negative"] - 0.70) < 1e-9
