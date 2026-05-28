"""Tests for the cross-session insights engine."""
from __future__ import annotations

import datetime as dt
from typing import Sequence

import pytest

from fluke_3540.events import Event, detect_events
from fluke_3540.insights import analyze
from fluke_3540.insights_compare import (
    CompareFinding, analyze_compare, to_jsonable,
)

from conftest import make_records, plant_window


def _session(label: str, records, start_offset_days: float = 0.0):
    """Build a session dict. Shifts every record start by start_offset_days."""
    shift = dt.timedelta(days=start_offset_days)
    shifted = [
        type(r)(index=r.index, start=r.start + shift, end=r.end + shift, floats=r.floats)
        for r in records
    ]
    events = detect_events(shifted, nominal_ln_v=277.0)
    findings = analyze(shifted, events)
    return {
        "label": label,
        "records": shifted,
        "events": events,
        "findings": findings,
    }


def _kind_set(findings: Sequence[CompareFinding]) -> set[str]:
    return {f.kind for f in findings}


def test_compare_returns_empty_on_one_session():
    s1 = _session("only", make_records(120))
    assert analyze_compare([s1]) == []


def test_compare_voltage_drift_rising():
    """Phase B rises 1 V per day across 3 captures."""
    sessions = []
    for i in range(3):
        ov = {}
        for n in range(120):
            ov[n] = {
                "V_LN_b_min_V": 277.0 + i,
                "V_LN_b_max_V": 277.0 + i,
                "V_LN_b_avg_V": 277.0 + i,
            }
        sessions.append(_session(f"d{i}", make_records(120, overrides=ov),
                                 start_offset_days=i))
    findings = analyze_compare(sessions)
    drift = [f for f in findings if f.kind == "voltage_drift"]
    assert drift, "expected at least one voltage_drift finding"
    b_drift = next(f for f in drift if "B" in f.headline)
    assert "rising" in b_drift.headline
    assert b_drift.session_labels == ("d0", "d1", "d2")


def test_compare_recurring_outages():
    """Two captures with outages at the same 22:00 window."""
    sessions = []
    for i in range(2):
        ov = {}
        # Plant an outage at index 60, which is 22:01 of the synthetic base.
        plant_window(ov, 60, 69, {
            "V_LN_a_min_V": 0, "V_LN_a_max_V": 0, "V_LN_a_avg_V": 0,
            "V_LN_b_min_V": 0, "V_LN_b_max_V": 0, "V_LN_b_avg_V": 0,
            "V_LN_c_min_V": 0, "V_LN_c_max_V": 0, "V_LN_c_avg_V": 0,
        })
        sessions.append(_session(f"day{i}", make_records(180, overrides=ov),
                                 start_offset_days=i))
    findings = analyze_compare(sessions)
    recurring = [f for f in findings if f.kind == "recurring_outages"]
    assert len(recurring) == 1
    assert "22:" in recurring[0].headline
    assert set(recurring[0].session_labels) == {"day0", "day1"}


def test_compare_pf_degradation_trending_up():
    """PF-low fraction climbs across 3 captures."""
    sessions = []
    for i in range(3):
        ov = {}
        # 20% of session at low PF in capture 0, 40% in capture 1, 60% in capture 2
        bad_count = (i + 1) * 24  # 24/40/72 of 120
        for n in range(bad_count):
            ov[n] = {"PF_total_avg": 0.55}
        sessions.append(_session(f"c{i}", make_records(120, overrides=ov),
                                 start_offset_days=i * 7))  # 1 week apart
    findings = analyze_compare(sessions)
    pf = [f for f in findings if f.kind == "pf_degradation"]
    assert pf, "expected pf_degradation finding"
    assert "degrading" in pf[0].headline.lower()


def test_compare_event_count_trend():
    """power_step events rising across captures."""
    sessions = []
    for i in range(3):
        ov = {}
        # Plant (i+1)*5 power-step events
        for k in range(5 * (i + 1)):
            idx = 10 + k * 2
            ov[idx] = {"P_total_avg_W": 200_000.0}
        sessions.append(_session(f"e{i}", make_records(120, overrides=ov),
                                 start_offset_days=i))
    findings = analyze_compare(sessions)
    trend = [f for f in findings if f.kind.startswith("event_trend_")]
    assert trend, "expected at least one event_trend finding"


def test_compare_sorted_by_severity_then_kind():
    """Two captures with phase drift + recurring outages — output is sorted."""
    sessions = []
    for i in range(2):
        ov = {}
        for n in range(120):
            ov[n] = {
                "V_LN_b_min_V": 277.0 + i * 3,
                "V_LN_b_max_V": 277.0 + i * 3,
                "V_LN_b_avg_V": 277.0 + i * 3,
            }
        plant_window(ov, 60, 69, {
            "V_LN_a_min_V": 0, "V_LN_a_max_V": 0, "V_LN_a_avg_V": 0,
            "V_LN_b_min_V": 0, "V_LN_b_max_V": 0, "V_LN_b_avg_V": 0,
            "V_LN_c_min_V": 0, "V_LN_c_max_V": 0, "V_LN_c_avg_V": 0,
        })
        sessions.append(_session(f"d{i}", make_records(180, overrides=ov),
                                 start_offset_days=i))
    findings = analyze_compare(sessions)
    sev_rank = {"alert": 0, "warn": 1, "info": 2}
    for i in range(1, len(findings)):
        assert sev_rank[findings[i].severity] >= sev_rank[findings[i - 1].severity]
    assert [f.id for f in findings] == list(range(len(findings)))


def test_compare_to_jsonable():
    f = CompareFinding(
        id=2, kind="voltage_drift", severity="warn",
        headline="X", detail="Y",
        session_labels=("a", "b"),
        recommended_actions=("do it",),
    )
    import json
    parsed = json.loads(json.dumps(to_jsonable(f)))
    assert parsed["session_labels"] == ["a", "b"]
    assert parsed["recommended_actions"] == ["do it"]
