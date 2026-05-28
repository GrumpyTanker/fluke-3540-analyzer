"""Tests for the TOU tariff calculator."""
from __future__ import annotations

import datetime as dt

import pytest

from fluke_3540.parser import FIELDS, Record
from fluke_3540.tariff import Tariff, compute_cost


_WH_IDX = next(f.index for f in FIELDS if f.name == "Wh_total")
_DATA_FLOATS = max(f.index for f in FIELDS) + 1


def _make_records(wh_per_hour: dict[int, float], hours: list[int]) -> list:
    """Build records at the given hours with given Wh_total values."""
    recs = []
    base = dt.datetime(2024, 1, 13, 0, 0, 0, tzinfo=dt.timezone.utc)
    for i, h in enumerate(hours):
        floats = [0.0] * 180
        floats[_WH_IDX] = wh_per_hour.get(h, 0.0)
        recs.append(Record(
            index=i,
            start=base + dt.timedelta(hours=h),
            end=base + dt.timedelta(hours=h, seconds=1),
            floats=tuple(floats),
        ))
    return recs


def test_tariff_is_peak_simple():
    t = Tariff(peak_hours=((9, 21),))
    assert not t.is_peak(8)
    assert t.is_peak(9)
    assert t.is_peak(15)
    assert t.is_peak(20)
    assert not t.is_peak(21)  # end is exclusive
    assert not t.is_peak(22)


def test_tariff_is_peak_wraps_midnight():
    # Peak from 22 → 06 next day (low-load nighttime industrial schedule)
    t = Tariff(peak_hours=((22, 6),))
    assert t.is_peak(23)
    assert t.is_peak(0)
    assert t.is_peak(5)
    assert not t.is_peak(6)
    assert not t.is_peak(12)
    assert t.is_peak(22)


def test_compute_cost_flat_offpeak_only():
    """No peak hours → everything bills at off-peak rate."""
    t = Tariff(offpeak_rate=0.10)
    # 10 records of 1000 Wh each = 10 kWh imported total
    recs = _make_records({h: 1000.0 for h in range(10)}, list(range(10)))
    cost = compute_cost(recs, t)
    assert cost["peak_kwh"] == 0
    assert cost["offpeak_kwh"] == pytest.approx(10.0)
    assert cost["imported_cost"] == pytest.approx(1.0)
    assert cost["exported_cost"] == 0
    assert cost["net_cost"] == pytest.approx(1.0)


def test_compute_cost_split_peak_offpeak():
    t = Tariff(peak_rate=0.20, offpeak_rate=0.05, peak_hours=((9, 17),))
    # 3 records inside peak (9, 12, 16) at 1 kWh each, 3 outside (7, 18, 21)
    recs = _make_records(
        {7: 1000, 9: 1000, 12: 1000, 16: 1000, 18: 1000, 21: 1000},
        [7, 9, 12, 16, 18, 21],
    )
    cost = compute_cost(recs, t)
    assert cost["peak_kwh"] == pytest.approx(3.0)
    assert cost["offpeak_kwh"] == pytest.approx(3.0)
    assert cost["peak_cost"] == pytest.approx(0.6)         # 3 * 0.20
    assert cost["offpeak_cost"] == pytest.approx(0.15)     # 3 * 0.05
    assert cost["imported_cost"] == pytest.approx(0.75)
    assert cost["net_cost"] == pytest.approx(0.75)


def test_compute_cost_export_subtracts():
    """Negative Wh (export) at the relevant rate produces a negative cost."""
    t = Tariff(offpeak_rate=0.10)
    recs = _make_records({0: -500.0, 1: 1000.0}, [0, 1])
    cost = compute_cost(recs, t)
    # imported: 1 kWh × 0.10 = 0.10; exported: -0.5 kWh × 0.10 = -0.05
    assert cost["imported_cost"] == pytest.approx(0.10)
    assert cost["exported_cost"] == pytest.approx(-0.05)
    assert cost["net_cost"] == pytest.approx(0.05)


def test_compute_cost_currency_passthrough():
    t = Tariff(currency="EUR", offpeak_rate=0.20)
    cost = compute_cost([], t)
    assert cost["currency"] == "EUR"


def test_compute_cost_empty_records():
    t = Tariff(offpeak_rate=0.10)
    cost = compute_cost([], t)
    assert cost["net_cost"] == 0
    assert cost["peak_kwh"] == 0
    assert cost["offpeak_kwh"] == 0
