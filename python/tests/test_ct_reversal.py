"""Tests for CT-reversal auto-detection (Feature C)."""
from __future__ import annotations

import pytest

from fluke_3540.analysis import ct_reversal_notice, detect_ct_reversal
from fluke_3540.store import ColumnStore

from conftest import make_records, plant_window


def test_healthy_load_not_flagged():
    # Positive P everywhere — a normal load.
    recs = make_records(200, defaults={"P_total_avg_W": 50_000.0})
    store = ColumnStore.from_records(recs)
    res = detect_ct_reversal(store)
    assert res["reversed"] is False
    assert res["frac_negative"] == 0.0
    assert res["mean_p_w"] > 0


def test_reversed_load_flagged():
    # Negative P everywhere (backwards CTs).
    recs = make_records(200, defaults={"P_total_avg_W": -50_000.0})
    store = ColumnStore.from_records(recs)
    res = detect_ct_reversal(store)
    assert res["reversed"] is True
    assert res["frac_negative"] == 1.0
    assert res["mean_p_w"] < 0
    assert res["negative_records"] == 200


def test_outage_samples_excluded():
    # 100 records: 50 negative-P load + a 50-sample outage (all V=0, P=0).
    overrides: dict = {}
    plant_window(overrides, 0, 49, {"P_total_avg_W": -40_000.0})
    plant_window(overrides, 50, 99, {
        "V_LN_a_avg_V": 0.0, "V_LN_b_avg_V": 0.0, "V_LN_c_avg_V": 0.0,
        "P_total_avg_W": 0.0,
    })
    recs = make_records(100, overrides=overrides)
    store = ColumnStore.from_records(recs)
    res = detect_ct_reversal(store)
    # Only the 50 non-outage records count; all of them are negative.
    assert res["non_outage_records"] == 50
    assert res["negative_records"] == 50
    assert res["frac_negative"] == 1.0
    assert res["reversed"] is True


def test_threshold_boundary():
    # 40% negative — below the default 60% threshold -> not flagged.
    overrides: dict = {}
    plant_window(overrides, 0, 39, {"P_total_avg_W": -10_000.0})
    plant_window(overrides, 40, 99, {"P_total_avg_W": 10_000.0})
    recs = make_records(100, overrides=overrides)
    store = ColumnStore.from_records(recs)
    res = detect_ct_reversal(store)
    assert abs(res["frac_negative"] - 0.40) < 1e-9
    assert res["reversed"] is False
    # Lowering the threshold flags it.
    res2 = detect_ct_reversal(store, neg_fraction_threshold=0.30)
    assert res2["reversed"] is True


def test_notice_is_loud_and_mentions_flags():
    recs = make_records(100, defaults={"P_total_avg_W": -50_000.0})
    store = ColumnStore.from_records(recs)
    res = detect_ct_reversal(store)
    notice = ct_reversal_notice(res)
    assert "CT REVERSAL DETECTED" in notice
    assert "--reverse-cts" in notice
    assert "--auto-reverse-cts" in notice


# --- magnitude-weighted (active-state) decision ------------------------------

def test_bimodal_decides_on_active_state_positive():
    # The real P115RE shape WITH --reverse-cts already correct: active draws
    # high current + POSITIVE power (correct), standby collapses to low current
    # + bogus NEGATIVE power. Whole-session count is 47% negative (near the
    # 50% line), but the ACTIVE state is clearly positive -> NOT reversed.
    overrides: dict = {}
    plant_window(overrides, 0, 48, {  # 49 active records, +97 kW, 239 A
        "I_a_avg_A": 239.0, "I_b_avg_A": 239.0, "I_c_avg_A": 239.0,
        "P_total_avg_W": 97_000.0})
    plant_window(overrides, 49, 99, {  # 51 standby records, -7.6 kW, 16 A
        "I_a_avg_A": 16.0, "I_b_avg_A": 16.0, "I_c_avg_A": 16.0,
        "P_total_avg_W": -7_600.0})
    store = ColumnStore.from_records(make_records(100, overrides=overrides))
    res = detect_ct_reversal(store)
    # whole-session count-based fraction is past 50% (the fragile signal)…
    assert res["frac_negative"] >= 0.50
    # …but the decision is made on the active state, which is positive.
    assert res["basis"] == "active"
    assert res["active_records"] == 49
    assert res["active_negative_records"] == 0
    assert res["active_frac_negative"] == 0.0
    assert res["active_mean_p_w"] == pytest.approx(97_000.0)
    assert res["reversed"] is False


def test_bimodal_decides_on_active_state_reversed():
    # Same bimodal shape but the ACTIVE state reads NEGATIVE (true backwards
    # CTs): active is the high-current state and it exports -> reversed=True,
    # even though standby happens to read small positive here.
    overrides: dict = {}
    plant_window(overrides, 0, 48, {  # active, -97 kW (backwards), 239 A
        "I_a_avg_A": 239.0, "I_b_avg_A": 239.0, "I_c_avg_A": 239.0,
        "P_total_avg_W": -97_000.0})
    plant_window(overrides, 49, 99, {  # standby, small +loss, 16 A
        "I_a_avg_A": 16.0, "I_b_avg_A": 16.0, "I_c_avg_A": 16.0,
        "P_total_avg_W": 500.0})
    store = ColumnStore.from_records(make_records(100, overrides=overrides))
    res = detect_ct_reversal(store)
    # whole-session count-based fraction is BELOW 50% (only active is negative)…
    assert res["frac_negative"] < 0.50
    # …but the active high-current state is fully negative -> reversed.
    assert res["basis"] == "active"
    assert res["active_frac_negative"] == pytest.approx(1.0)
    assert res["reversed"] is True


def test_no_active_population_falls_back_to_whole_session():
    # Everything below the active threshold -> no active state -> the legacy
    # whole-session count-based decision is used.
    store = ColumnStore.from_records(make_records(100, defaults={
        "I_a_avg_A": 10.0, "I_b_avg_A": 10.0, "I_c_avg_A": 10.0,
        "P_total_avg_W": -5_000.0}))
    res = detect_ct_reversal(store)
    assert res["basis"] == "whole_session"
    assert res["active_records"] == 0
    assert res["reversed"] is True  # 100% of non-outage is negative


def test_active_threshold_configurable():
    # 30 A current with the default 50 A threshold -> standby (fallback);
    # lower the threshold to 20 A -> those become active and drive the decision.
    store = ColumnStore.from_records(make_records(100, defaults={
        "I_a_avg_A": 30.0, "I_b_avg_A": 30.0, "I_c_avg_A": 30.0,
        "P_total_avg_W": -5_000.0}))
    res_default = detect_ct_reversal(store)
    assert res_default["basis"] == "whole_session"
    res_low = detect_ct_reversal(store, active_threshold_a=20.0)
    assert res_low["basis"] == "active"
    assert res_low["active_records"] == 100
