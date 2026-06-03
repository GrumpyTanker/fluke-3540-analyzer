"""Tests for CT-reversal auto-detection (Feature C)."""
from __future__ import annotations

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
