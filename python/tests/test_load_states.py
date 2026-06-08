"""Tests for the active/standby load-state split (current-gated).

Real bimodal loads (a coating rectifier) alternate between a heavy ACTIVE draw
and a light STANDBY state. We classify each record by mean per-phase CURRENT,
report the two states separately, surface the active-state PF, and correct the
session energy three ways.
"""
from __future__ import annotations

import datetime as dt

import pytest

from fluke_3540.analysis import (
    LOAD_STATES,
    STANDBY_CURRENT_THRESHOLD_A,
    active_state_pf,
    classify_load_states,
    load_state_rows,
    session_energy,
)
from fluke_3540.parser import Record
from fluke_3540.store import ColumnStore

from conftest import make_records, plant_window


def _bimodal_store(active_n=50, standby_n=50,
                   active_i=239.0, standby_i=16.0,
                   active_p=97_000.0, standby_p=-7_600.0,
                   active_pf=0.47, standby_pf=-0.64):
    """A balanced bimodal P115RE-like session: an active +cluster and a standby
    -cluster, classified by current. Active records draw high current + positive
    power; standby collapses to low current + (bogus) negative power."""
    overrides: dict = {}
    plant_window(overrides, 0, active_n - 1, {
        "I_a_avg_A": active_i, "I_b_avg_A": active_i, "I_c_avg_A": active_i,
        "P_total_avg_W": active_p, "S_total_avg_VA": active_p / active_pf,
        "PF_total_avg": active_pf,
    })
    plant_window(overrides, active_n, active_n + standby_n - 1, {
        "I_a_avg_A": standby_i, "I_b_avg_A": standby_i, "I_c_avg_A": standby_i,
        "P_total_avg_W": standby_p, "S_total_avg_VA": abs(standby_p / standby_pf),
        "PF_total_avg": standby_pf,
    })
    recs = make_records(active_n + standby_n, overrides=overrides)
    return ColumnStore.from_records(recs)


# --- classifier --------------------------------------------------------------

def test_classify_threshold_default():
    store = _bimodal_store(active_n=50, standby_n=50)
    groups = classify_load_states(store)  # default 50 A
    assert groups["active"] == list(range(0, 50))
    assert groups["standby"] == list(range(50, 100))


def test_classify_threshold_configurable():
    # With a 20 A threshold the 16 A standby is still standby; with a 10 A
    # threshold it becomes active.
    store = _bimodal_store(standby_i=16.0)
    g20 = classify_load_states(store, threshold_a=20.0)
    assert len(g20["active"]) == 50 and len(g20["standby"]) == 50
    g10 = classify_load_states(store, threshold_a=10.0)
    assert len(g10["active"]) == 100 and g10["standby"] == []


def test_classify_uses_mean_of_three_phases():
    # One phase high, two phases zero -> mean 80 A -> active at default 50 A.
    overrides = {0: {"I_a_avg_A": 240.0, "I_b_avg_A": 0.0, "I_c_avg_A": 0.0}}
    store = ColumnStore.from_records(make_records(1, overrides=overrides))
    g = classify_load_states(store)
    assert g["active"] == [0]
    # Drop it below: 120 A on one phase -> mean 40 A -> standby.
    overrides = {0: {"I_a_avg_A": 120.0, "I_b_avg_A": 0.0, "I_c_avg_A": 0.0}}
    store = ColumnStore.from_records(make_records(1, overrides=overrides))
    g = classify_load_states(store)
    assert g["standby"] == [0]


def test_classify_boundary_inclusive():
    # Exactly at threshold counts as active (>=).
    overrides = {0: {"I_a_avg_A": 50.0, "I_b_avg_A": 50.0, "I_c_avg_A": 50.0}}
    store = ColumnStore.from_records(make_records(1, overrides=overrides))
    assert classify_load_states(store, threshold_a=50.0)["active"] == [0]


# --- load_state_rows ---------------------------------------------------------

def test_load_state_rows_schema_and_order():
    store = _bimodal_store()
    rows = load_state_rows(store)
    assert [r["state"] for r in rows] == list(LOAD_STATES) == ["active", "standby"]
    for r in rows:
        for k in ("state", "records", "hours", "duty_pct", "kWh", "P_avg_kW",
                  "P_min_kW", "P_max_kW", "I_avg_A", "S_avg_kVA", "PF_avg",
                  "V_LN_avg_V", "V_THD_p95_pct"):
            assert k in r, f"missing schema key {k}"


def test_load_state_rows_values():
    store = _bimodal_store(active_n=50, standby_n=50,
                           active_i=239.0, standby_i=16.0,
                           active_p=97_000.0, standby_p=-7_600.0,
                           active_pf=0.47, standby_pf=-0.64)
    rows = load_state_rows(store)
    by = {r["state"]: r for r in rows}
    a, s = by["active"], by["standby"]
    assert a["records"] == 50 and s["records"] == 50
    assert a["duty_pct"] == pytest.approx(50.0)
    assert s["duty_pct"] == pytest.approx(50.0)
    assert a["I_avg_A"] == pytest.approx(239.0)
    assert s["I_avg_A"] == pytest.approx(16.0)
    assert a["P_avg_kW"] == pytest.approx(97.0)
    assert s["P_avg_kW"] == pytest.approx(-7.6)
    assert a["PF_avg"] == pytest.approx(0.47)
    assert s["PF_avg"] == pytest.approx(-0.64)
    # active-state energy: 97 kW * 50 s = 50/3600 h
    assert a["kWh"] == pytest.approx(97.0 * (50 / 3600.0))


def test_active_state_pf_helper():
    store = _bimodal_store(active_pf=0.47)
    rows = load_state_rows(store)
    assert active_state_pf(rows) == pytest.approx(0.47)
    # No active records -> None.
    standby_only = ColumnStore.from_records(
        make_records(10, defaults={"I_a_avg_A": 5.0, "I_b_avg_A": 5.0,
                                   "I_c_avg_A": 5.0}))
    rows2 = load_state_rows(standby_only)
    assert active_state_pf(rows2) == 0.0  # active row present but empty -> 0.0


# --- three energy figures ----------------------------------------------------

def test_three_energy_figures_distinct():
    # 50 active @ +97 kW, 50 standby @ -7.6 kW, 1 s each.
    store = _bimodal_store(active_n=50, standby_n=50,
                           active_p=97_000.0, standby_p=-7_600.0)
    e = session_energy(store)
    per_s = 1.0 / 1000.0 / 3600.0
    as_measured = (50 * 97_000.0 + 50 * -7_600.0) * per_s
    active = 50 * 97_000.0 * per_s
    clip = 50 * 97_000.0 * per_s  # standby negative clipped to 0
    assert e["energy_as_measured_kWh"] == pytest.approx(as_measured)
    assert e["energy_active_kWh"] == pytest.approx(active)
    assert e["energy_net_clip_standby_kWh"] == pytest.approx(clip)
    # The understated as-measured < the corrected figures.
    assert e["energy_as_measured_kWh"] < e["energy_active_kWh"]
    assert e["energy_as_measured_kWh"] < e["energy_net_clip_standby_kWh"]
    assert e["standby_threshold_a"] == STANDBY_CURRENT_THRESHOLD_A
    assert "unreliable" in e["note"].lower()


def test_clip_keeps_positive_standby():
    # If standby draws small POSITIVE losses, clip == as_measured == active+standby.
    store = _bimodal_store(active_p=97_000.0, standby_p=2_000.0,
                           standby_pf=0.3)
    e = session_energy(store)
    assert e["energy_net_clip_standby_kWh"] == pytest.approx(
        e["energy_as_measured_kWh"])
    assert e["energy_active_kWh"] < e["energy_net_clip_standby_kWh"]


def test_energy_skips_nonfinite_p():
    overrides = {
        0: {"I_a_avg_A": 200.0, "I_b_avg_A": 200.0, "I_c_avg_A": 200.0,
            "P_total_avg_W": 100_000.0},
        1: {"I_a_avg_A": 200.0, "I_b_avg_A": 200.0, "I_c_avg_A": 200.0,
            "P_total_avg_W": float("nan")},
    }
    store = ColumnStore.from_records(make_records(2, overrides=overrides))
    e = session_energy(store)
    per_s = 1.0 / 1000.0 / 3600.0
    assert e["energy_active_kWh"] == pytest.approx(100_000.0 * per_s)
