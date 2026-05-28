"""Tests for event detection — plants known events in synthetic records."""
from __future__ import annotations

import datetime as dt

import pytest

from fluke_3540.events import DEFAULT_RULES, EventRules, detect_events

from conftest import make_records, plant_window


def test_no_events_on_flat_healthy_data():
    recs = make_records(120)
    assert detect_events(recs) == []


def test_outage_detected():
    overrides: dict[int, dict[str, float]] = {}
    # 10-second total outage starting at second 60
    plant_window(overrides, 60, 69, {
        "V_LN_a_min_V": 0.0, "V_LN_a_max_V": 0.0, "V_LN_a_avg_V": 0.0,
        "V_LN_b_min_V": 0.0, "V_LN_b_max_V": 0.0, "V_LN_b_avg_V": 0.0,
        "V_LN_c_min_V": 0.0, "V_LN_c_max_V": 0.0, "V_LN_c_avg_V": 0.0,
    })
    recs = make_records(180, overrides=overrides)
    events = detect_events(recs, nominal_ln_v=277.0)
    outages = [e for e in events if e.kind == "outage"]
    assert len(outages) == 1
    o = outages[0]
    assert o.t_start == recs[60].start
    # Allow gap-tolerance to extend by ≤ 1 sec
    assert o.t_end >= recs[69].end - dt.timedelta(seconds=1)
    assert o.t_end <= recs[70].end
    assert o.severity == 0.0


def test_dip_detected_phase_specific():
    overrides: dict[int, dict[str, float]] = {}
    # phase A drops to 200 V_LN min for 5 sec — below 0.9 * 277 = 249.3
    plant_window(overrides, 30, 34, {"V_LN_a_min_V": 200.0})
    recs = make_records(120, overrides=overrides)
    events = detect_events(recs, nominal_ln_v=277.0)
    dips = [e for e in events if e.kind == "dip"]
    assert len(dips) == 1
    d = dips[0]
    assert d.affected_phases == ("a",)
    assert d.t_start == recs[30].start
    assert pytest.approx(d.severity, rel=1e-3) == 200.0 / 277.0


def test_swell_detected_phase_specific():
    overrides: dict[int, dict[str, float]] = {}
    # phase B swells to 320 V_LN max for 3 sec — above 1.1 * 277 = 304.7
    plant_window(overrides, 50, 52, {"V_LN_b_max_V": 320.0})
    recs = make_records(120, overrides=overrides)
    events = detect_events(recs, nominal_ln_v=277.0)
    swells = [e for e in events if e.kind == "swell"]
    assert len(swells) == 1
    s = swells[0]
    assert s.affected_phases == ("b",)
    assert pytest.approx(s.severity, rel=1e-3) == 320.0 / 277.0


def test_dip_excludes_outage():
    overrides: dict[int, dict[str, float]] = {}
    # Total outage from 50..59 — phase A min reads 0 in this window, which is
    # also below the dip threshold. detect_events must classify this as outage
    # only, not as both.
    plant_window(overrides, 50, 59, {
        "V_LN_a_min_V": 0.0, "V_LN_a_max_V": 0.0, "V_LN_a_avg_V": 0.0,
        "V_LN_b_min_V": 0.0, "V_LN_b_max_V": 0.0, "V_LN_b_avg_V": 0.0,
        "V_LN_c_min_V": 0.0, "V_LN_c_max_V": 0.0, "V_LN_c_avg_V": 0.0,
    })
    recs = make_records(120, overrides=overrides)
    events = detect_events(recs, nominal_ln_v=277.0)
    assert {e.kind for e in events} == {"outage"}


def test_high_current_excursion():
    overrides: dict[int, dict[str, float]] = {}
    # 1-sec phase C peak of 500A in otherwise-steady 100A series.
    overrides[80] = {"I_c_max_A": 500.0}
    recs = make_records(200, overrides=overrides)
    events = detect_events(recs, nominal_ln_v=277.0)
    spikes = [e for e in events if e.kind == "high_current"]
    assert len(spikes) >= 1
    sc = next(e for e in spikes if "c" in e.affected_phases)
    assert sc.severity == 500.0


def test_freq_excursion():
    overrides: dict[int, dict[str, float]] = {}
    plant_window(overrides, 40, 41, {"freq_avg_Hz": 60.7})  # +0.7 Hz
    recs = make_records(120, overrides=overrides)
    events = detect_events(recs, nominal_ln_v=277.0)
    fe = [e for e in events if e.kind == "freq_excursion"]
    assert len(fe) == 1
    assert fe[0].severity == pytest.approx(0.7, abs=1e-3)


def test_imbalance_spike():
    overrides: dict[int, dict[str, float]] = {}
    # phase voltages 277 / 277 / 260 -> max-min = 17 V, mean ≈ 271.3, % ≈ 6.27%
    plant_window(overrides, 30, 34, {"V_LN_c_avg_V": 260.0})
    recs = make_records(120, overrides=overrides)
    events = detect_events(recs, nominal_ln_v=277.0)
    imb = [e for e in events if e.kind == "imbalance_spike"]
    assert len(imb) == 1
    assert imb[0].severity > 2.5


def test_power_step_detected():
    overrides: dict[int, dict[str, float]] = {}
    # Step P_total from 50_000 to 200_000 at second 60 — a 150kW step is >> 50%
    # of the (mostly 50kW) session mean.
    for i in range(60, 120):
        overrides[i] = {"P_total_avg_W": 200_000.0}
    recs = make_records(120, overrides=overrides)
    events = detect_events(recs, nominal_ln_v=277.0)
    steps = [e for e in events if e.kind == "power_step"]
    assert len(steps) == 1
    assert steps[0].severity > 0  # import step


def test_nominal_voltage_auto_inference():
    # Mostly 277V samples, with a brief outage. Median should still be 277.
    overrides: dict[int, dict[str, float]] = {}
    plant_window(overrides, 50, 59, {
        "V_LN_a_avg_V": 0.0, "V_LN_b_avg_V": 0.0, "V_LN_c_avg_V": 0.0,
        "V_LN_a_min_V": 0.0, "V_LN_b_min_V": 0.0, "V_LN_c_min_V": 0.0,
    })
    recs = make_records(200, overrides=overrides)
    # Don't pass nominal_ln_v — let it auto-infer.
    events = detect_events(recs)
    # We should detect the outage only (no swells, since 277V is the median).
    assert {e.kind for e in events} == {"outage"}


def test_events_assigned_sequential_ids_in_time_order():
    overrides: dict[int, dict[str, float]] = {}
    # Earlier swell
    plant_window(overrides, 10, 12, {"V_LN_a_max_V": 320.0})
    # Later dip
    plant_window(overrides, 80, 84, {"V_LN_b_min_V": 200.0})
    recs = make_records(200, overrides=overrides)
    events = detect_events(recs, nominal_ln_v=277.0)
    assert [e.id for e in events] == list(range(len(events)))
    assert events[0].t_start < events[-1].t_start


def test_empty_records_returns_empty():
    assert detect_events([]) == []


def test_custom_rules_override():
    from dataclasses import replace
    overrides: dict[int, dict[str, float]] = {}
    # 3-sec V_LN_a_max swell to 295.0 — exceeds 105% (290.85) but not 110% (304.7).
    plant_window(overrides, 30, 32, {"V_LN_a_max_V": 295.0})
    recs = make_records(120, overrides=overrides)
    # With default rules: no swell
    assert all(e.kind != "swell" for e in detect_events(recs, nominal_ln_v=277.0))
    # With looser rules (105%): swell detected
    looser = replace(DEFAULT_RULES, swell_pct_of_nominal=1.05)
    swells = [e for e in detect_events(recs, nominal_ln_v=277.0, rules=looser)
              if e.kind == "swell"]
    assert len(swells) == 1
