"""Tests for the Insights engine."""
from __future__ import annotations

import datetime as dt
from typing import Sequence

import pytest

from fluke_3540.events import Event, detect_events
from fluke_3540.insights import Finding, analyze, to_jsonable
from fluke_3540.parser import Record

from conftest import make_records, plant_window


def _phase_asymmetry_records(b_offset_v: float = 8.0) -> Sequence[Record]:
    """Healthy records with phase B running hot by `b_offset_v` volts."""
    overrides: dict[int, dict[str, float]] = {}
    for i in range(120):
        overrides[i] = {
            "V_LN_b_min_V": 277.0 + b_offset_v,
            "V_LN_b_max_V": 277.0 + b_offset_v,
            "V_LN_b_avg_V": 277.0 + b_offset_v,
        }
    return make_records(120, overrides=overrides)


def _kind_set(findings: Sequence[Finding]) -> set[str]:
    return {f.kind for f in findings}


def test_analyze_returns_empty_on_healthy_data():
    findings = analyze(make_records(120), events=[])
    # No events, no asymmetry, no PF drift -> nothing to flag
    assert findings == []


def test_phase_asymmetry_warn():
    recs = _phase_asymmetry_records(b_offset_v=8.0)
    findings = analyze(recs, events=[])
    assert "phase_asymmetry" in _kind_set(findings)
    pa = next(f for f in findings if f.kind == "phase_asymmetry")
    assert pa.severity in ("warn", "alert")
    assert "B" in pa.headline.upper() or "phase b" in pa.headline.lower()


def test_phase_asymmetry_below_threshold_no_finding():
    recs = _phase_asymmetry_records(b_offset_v=2.0)  # ~0.7% spread
    findings = analyze(recs, events=[])
    assert "phase_asymmetry" not in _kind_set(findings)


def test_outage_signature_with_leading_dip():
    # Plant a leading dip on phase A, then a 10s all-phase outage.
    overrides: dict[int, dict[str, float]] = {}
    plant_window(overrides, 58, 59, {"V_LN_a_min_V": 200.0})  # leading dip
    plant_window(overrides, 60, 69, {
        "V_LN_a_min_V": 0, "V_LN_a_max_V": 0, "V_LN_a_avg_V": 0,
        "V_LN_b_min_V": 0, "V_LN_b_max_V": 0, "V_LN_b_avg_V": 0,
        "V_LN_c_min_V": 0, "V_LN_c_max_V": 0, "V_LN_c_avg_V": 0,
    })
    recs = make_records(180, overrides=overrides)
    events = detect_events(recs, nominal_ln_v=277.0)
    findings = analyze(recs, events=events)
    sig = [f for f in findings if f.kind == "outage_signature"]
    assert len(sig) == 1
    # The signature finding should reference both the outage and the dip
    assert len(sig[0].related_event_ids) >= 2
    assert "leading dip" in sig[0].detail


def test_outage_signature_lonely_outage_has_no_correlated_events():
    # 10-sec outage with no surrounding dip or inrush in the same window
    overrides: dict[int, dict[str, float]] = {}
    plant_window(overrides, 60, 69, {
        "V_LN_a_min_V": 0, "V_LN_a_max_V": 0, "V_LN_a_avg_V": 0,
        "V_LN_b_min_V": 0, "V_LN_b_max_V": 0, "V_LN_b_avg_V": 0,
        "V_LN_c_min_V": 0, "V_LN_c_max_V": 0, "V_LN_c_avg_V": 0,
    })
    recs = make_records(180, overrides=overrides)
    events = detect_events(recs, nominal_ln_v=277.0)
    findings = analyze(recs, events=events)
    sig = next(f for f in findings if f.kind == "outage_signature")
    assert len(sig.related_event_ids) == 1
    assert "hard supply loss" in sig.detail or "hard" in sig.detail.lower()


def test_pf_drift_recommends_kvar_sizing():
    # Plant 30% of the session with low PF and high Q
    overrides: dict[int, dict[str, float]] = {}
    for i in range(40, 80):
        overrides[i] = {
            "PF_total_avg": 0.60,
            "Q_total_avg_VAR": 30_000.0,
            "S_total_avg_VA": 80_000.0,
        }
    recs = make_records(120, overrides=overrides)
    findings = analyze(recs, events=[])
    pf = next(f for f in findings if f.kind == "pf_drift")
    assert "below 0.85" in pf.headline
    # Recommendation should mention kVAR
    assert any("kVAR" in a for a in pf.recommended_actions)


def test_pf_drift_below_min_fraction_no_finding():
    overrides: dict[int, dict[str, float]] = {}
    # Only 2 seconds of low PF out of 120 — far below the 10% threshold
    overrides[10] = {"PF_total_avg": 0.6}
    overrides[11] = {"PF_total_avg": 0.6}
    recs = make_records(120, overrides=overrides)
    findings = analyze(recs, events=[])
    assert "pf_drift" not in _kind_set(findings)


def test_imbalance_sustained_detected():
    # Sustained 2% imbalance for 70 seconds — should trigger (default 1.5% / 60 s)
    overrides: dict[int, dict[str, float]] = {}
    plant_window(overrides, 20, 89, {
        "V_LN_a_avg_V": 277.0,
        "V_LN_b_avg_V": 277.0,
        "V_LN_c_avg_V": 272.0,  # ~1.8% imbalance vs 275 mean
    })
    recs = make_records(180, overrides=overrides)
    findings = analyze(recs, events=[])
    assert "imbalance_sustained" in _kind_set(findings)


def test_outage_frequency_alert_when_high():
    # Two outages within a session that's just over 1 day long → rate > 1/day
    overrides: dict[int, dict[str, float]] = {}
    plant_window(overrides, 1000, 1009, {
        "V_LN_a_min_V": 0, "V_LN_a_max_V": 0, "V_LN_a_avg_V": 0,
        "V_LN_b_min_V": 0, "V_LN_b_max_V": 0, "V_LN_b_avg_V": 0,
        "V_LN_c_min_V": 0, "V_LN_c_max_V": 0, "V_LN_c_avg_V": 0,
    })
    plant_window(overrides, 50_000, 50_009, {
        "V_LN_a_min_V": 0, "V_LN_a_max_V": 0, "V_LN_a_avg_V": 0,
        "V_LN_b_min_V": 0, "V_LN_b_max_V": 0, "V_LN_b_avg_V": 0,
        "V_LN_c_min_V": 0, "V_LN_c_max_V": 0, "V_LN_c_avg_V": 0,
    })
    recs = make_records(86_400, overrides=overrides)  # ~1 day session
    events = detect_events(recs, nominal_ln_v=277.0)
    findings = analyze(recs, events=events)
    of = next((f for f in findings if f.kind == "outage_frequency"), None)
    assert of is not None
    assert "/day" in of.headline


def test_current_spike_ratio_info():
    overrides: dict[int, dict[str, float]] = {}
    # Otherwise-flat 50 A on phase A; one spike of 400 A → 8× ratio
    for i in range(200):
        overrides[i] = {"I_a_max_A": 50.0, "I_b_max_A": 50.0, "I_c_max_A": 50.0}
    overrides[100] = {"I_a_max_A": 400.0, "I_b_max_A": 50.0, "I_c_max_A": 50.0}
    recs = make_records(200, overrides=overrides)
    findings = analyze(recs, events=[])
    spikes = [f for f in findings if f.kind == "current_spike_ratio"]
    # Only phase A should fire (the other two are flat)
    assert any("A" in f.headline for f in spikes)
    assert all(f.severity == "info" for f in spikes)


def test_findings_sorted_by_severity_then_kind():
    overrides: dict[int, dict[str, float]] = {}
    plant_window(overrides, 0, 119, {
        "V_LN_b_avg_V": 290.0,
        "V_LN_b_min_V": 290.0,
        "V_LN_b_max_V": 290.0,
        "PF_total_avg": 0.60,
        "Q_total_avg_VAR": 30_000.0,
        "S_total_avg_VA": 80_000.0,
    })
    recs = make_records(120, overrides=overrides)
    findings = analyze(recs, events=[])
    sev_rank = {"alert": 0, "warn": 1, "info": 2}
    for i in range(1, len(findings)):
        assert sev_rank[findings[i].severity] >= sev_rank[findings[i - 1].severity]
    assert [f.id for f in findings] == list(range(len(findings)))


def test_to_jsonable_roundtrips_basic_fields():
    f = Finding(
        id=3, kind="phase_asymmetry", severity="warn",
        headline="X", detail="Y",
        related_event_ids=(1, 2),
        recommended_actions=("a",),
    )
    import json
    text = json.dumps(to_jsonable(f))
    parsed = json.loads(text)
    assert parsed["id"] == 3
    assert parsed["related_event_ids"] == [1, 2]
    assert parsed["recommended_actions"] == ["a"]
