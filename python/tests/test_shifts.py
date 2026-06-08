"""Tests for generalized, named, configurable shift/period splitting.

The shift model lets users define multiple named time windows within a day
(which may wrap past midnight) and:
  * AGGREGATE all records of each named shift across the whole session into a
    single comparison row (day vs night, A/B/C shifts, …);
  * bucket each individual shift OCCURRENCE as its own contiguous time-ordered
    range (a "night" spanning midnight is ONE occurrence labeled by its start
    date).

CRITICAL tz contract: the store holds UTC timestamps; shift windows are
evaluated in the *report* timezone (``--tz``). The localize step happens before
the HH:MM window rule is applied.
"""
from __future__ import annotations

import datetime as dt

import pytest

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from fluke_3540.analysis import (
    Shift,
    ShiftSet,
    aggregate_shifts,
    gather_store,
    shift_comparison_rows,
    shift_occurrences,
)
from fluke_3540.parser import Record
from fluke_3540.store import ColumnStore

from conftest import make_records


def make_minute_records(count, base, overrides=None, defaults=None):
    """make_records spaced one MINUTE apart (the per-second helper packs all
    records into the same minute-of-day, which the shift logic keys on)."""
    recs = make_records(count, base=base, overrides=overrides, defaults=defaults)
    out = []
    for n, r in enumerate(recs):
        start = base + dt.timedelta(minutes=n)
        out.append(Record(index=n, start=start,
                          end=start + dt.timedelta(minutes=1), floats=r.floats))
    return out


# --- ShiftSet.parse ----------------------------------------------------------

def test_parse_two_shifts():
    ss = ShiftSet.parse("day=06:00-18:00,night=18:00-06:00")
    assert [s.name for s in ss.shifts] == ["day", "night"]
    day, night = ss.shifts
    assert (day.start_min, day.end_min) == (360, 1080)
    assert day.wraps is False
    assert (night.start_min, night.end_min) == (1080, 360)
    assert night.wraps is True


def test_parse_three_shifts():
    ss = ShiftSet.parse("A=06:00-14:00,B=14:00-22:00,C=22:00-06:00")
    assert [s.name for s in ss.shifts] == ["A", "B", "C"]
    assert ss.shifts[2].wraps is True  # C crosses midnight


def test_parse_default():
    ss = ShiftSet.default()
    assert [s.name for s in ss.shifts] == ["day", "night"]
    assert ss.shifts[0].start_min == 360


def test_parse_bad_time():
    with pytest.raises(ValueError):
        ShiftSet.parse("day=06:00-25:00")
    with pytest.raises(ValueError):
        ShiftSet.parse("day=0600-1800")  # missing colon → not HH:MM
    with pytest.raises(ValueError):
        ShiftSet.parse("garbage")


def test_parse_duplicate_names():
    with pytest.raises(ValueError):
        ShiftSet.parse("day=06:00-12:00,day=12:00-18:00")


def test_parse_zero_length_window_rejected():
    with pytest.raises(ValueError):
        ShiftSet.parse("x=06:00-06:00")


def test_load_shifts_file(tmp_path):
    from fluke_3540.shifts_file import load_shifts
    p = tmp_path / "s.json"
    p.write_text('{"shifts":[{"name":"day","start":"06:00","end":"18:00"},'
                 '{"name":"night","start":"18:00","end":"06:00"}]}',
                 encoding="utf-8")
    ss = load_shifts(p)
    assert [s.name for s in ss.shifts] == ["day", "night"]


def test_load_shifts_file_bare_list(tmp_path):
    from fluke_3540.shifts_file import load_shifts
    p = tmp_path / "s.json"
    p.write_text('[{"name":"A","start":"06:00","end":"18:00"}]', encoding="utf-8")
    ss = load_shifts(p)
    assert [s.name for s in ss.shifts] == ["A"]


def test_load_shifts_file_bad(tmp_path):
    from fluke_3540.shifts_file import load_shifts
    p = tmp_path / "s.json"
    p.write_text('{"nope": 1}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_shifts(p)


def test_from_spec_dicts():
    ss = ShiftSet.from_spec([
        {"name": "day", "start": "06:00", "end": "18:00"},
        {"name": "night", "start": "18:00", "end": "06:00"},
    ])
    assert [s.name for s in ss.shifts] == ["day", "night"]
    assert ss.shifts[1].wraps is True


# --- Shift.contains_minute (wrap logic) --------------------------------------

def test_contains_minute_non_wrapping():
    day = Shift("day", 360, 1080)
    assert day.contains_minute(360) is True   # 06:00 inclusive
    assert day.contains_minute(1079) is True  # 17:59
    assert day.contains_minute(1080) is False  # 18:00 exclusive
    assert day.contains_minute(300) is False


def test_contains_minute_wrapping():
    night = Shift("night", 1080, 360)
    assert night.contains_minute(1080) is True   # 18:00
    assert night.contains_minute(1439) is True   # 23:59
    assert night.contains_minute(0) is True      # 00:00
    assert night.contains_minute(359) is True    # 05:59
    assert night.contains_minute(360) is False   # 06:00 → day
    assert night.contains_minute(720) is False   # noon


# --- tz-localized assignment -------------------------------------------------

@pytest.mark.skipif(ZoneInfo is None, reason="zoneinfo unavailable")
def test_assignment_localizes_to_report_tz():
    # A record stored at 10:30 UTC is 05:30 America/Chicago (CDT, UTC-5).
    # In Central that lands in the night shift (the 00:00-06:00 wrap leg); in
    # UTC (10:30, mod 630) it would land in the day shift. This pins the tz
    # contract: windows are evaluated in the report tz, not raw UTC.
    ss = ShiftSet.parse("day=06:00-18:00,night=18:00-06:00")
    central = ZoneInfo("America/Chicago")
    base = dt.datetime(2026, 5, 29, 10, 30, 0, tzinfo=dt.timezone.utc)
    recs = make_records(1, base=base)
    store = ColumnStore.from_records(recs)

    by_name_central = aggregate_shifts(store, ss, tz=central)
    assert by_name_central["night"] == [0]
    assert by_name_central.get("day", []) == []

    by_name_utc = aggregate_shifts(store, ss, tz=None)
    assert by_name_utc["day"] == [0]
    assert by_name_utc.get("night", []) == []


def test_assignment_utc_default():
    # 08:00 UTC, no tz → day shift.
    ss = ShiftSet.parse("day=06:00-18:00,night=18:00-06:00")
    base = dt.datetime(2026, 5, 29, 8, 0, 0, tzinfo=dt.timezone.utc)
    recs = make_records(1, base=base)
    store = ColumnStore.from_records(recs)
    by_name = aggregate_shifts(store, ss, tz=None)
    assert by_name["day"] == [0]


# --- aggregate_shifts grouping (non-contiguous) ------------------------------

def test_aggregate_groups_noncontiguous_indices():
    # 06:00 + a few hours of records straddling into night, all 1/sec. Build a
    # compact multi-window day: 11 records starting 17:58:00 UTC → 17:58..18:08.
    # Records before 18:00 → day; 18:00 onward → night. So indices are split
    # into two contiguous chunks here, but the API returns lists by name.
    ss = ShiftSet.parse("day=06:00-18:00,night=18:00-06:00")
    base = dt.datetime(2026, 5, 29, 17, 58, 0, tzinfo=dt.timezone.utc)
    recs = make_minute_records(11, base=base)  # 17:58 .. 18:08 starts
    store = ColumnStore.from_records(recs)
    by_name = aggregate_shifts(store, ss, tz=None)
    # 17:58:00, 17:59:00 (mins 1078,1079) are day; 18:00:00.. are night.
    assert by_name["day"] == [0, 1]
    assert by_name["night"] == list(range(2, 11))


def test_gather_store_picks_indices():
    recs = make_records(50, defaults={"P_total_avg_W": 1000.0})
    store = ColumnStore.from_records(recs, time_shift=dt.timedelta(hours=2))
    sub = gather_store(store, [3, 7, 40])
    assert sub.n == 3
    assert sub.time_shift == dt.timedelta(hours=2)
    assert sub.start(0) == store.start(3)
    assert sub.start(1) == store.start(7)
    assert sub.col("P_total_avg_W")[2] == store.col("P_total_avg_W")[40]


# --- unassigned + overlap/gap validation -------------------------------------

def test_unassigned_when_window_does_not_tile():
    # Only a morning shift; afternoon records have no home → "unassigned".
    ss = ShiftSet.parse("morning=06:00-12:00")
    base = dt.datetime(2026, 5, 29, 11, 58, 0, tzinfo=dt.timezone.utc)
    recs = make_minute_records(5, base=base)  # 11:58..12:02
    store = ColumnStore.from_records(recs)
    by_name = aggregate_shifts(store, ss, tz=None)
    assert by_name["morning"] == [0, 1]            # 11:58, 11:59
    assert by_name["unassigned"] == [2, 3, 4]      # 12:00, 12:01, 12:02


def test_first_matching_window_wins_on_overlap():
    # Overlapping windows: record at 10:00 matches both; the first listed wins.
    ss = ShiftSet.parse("early=06:00-12:00,late=10:00-18:00")
    base = dt.datetime(2026, 5, 29, 10, 0, 0, tzinfo=dt.timezone.utc)
    recs = make_records(1, base=base)
    store = ColumnStore.from_records(recs)
    by_name = aggregate_shifts(store, ss, tz=None)
    assert by_name["early"] == [0]
    assert by_name.get("late", []) == []


def test_coverage_gaps_and_overlaps_reported():
    full = ShiftSet.parse("day=06:00-18:00,night=18:00-06:00")
    assert full.coverage_issues() == []  # tiles 24 h cleanly

    gapped = ShiftSet.parse("morning=06:00-12:00,evening=14:00-20:00")
    issues = gapped.coverage_issues()
    assert any("gap" in s.lower() for s in issues)

    overlapped = ShiftSet.parse("early=06:00-13:00,late=12:00-18:00")
    issues = overlapped.coverage_issues()
    assert any("overlap" in s.lower() for s in issues)


# --- occurrences (contiguous per-instance buckets) ---------------------------

def test_shift_occurrences_labels_by_start_date():
    # Night window 18:00-06:00; records 17:58:00..18:03:00 UTC → the records at
    # 18:00+ are one "night" occurrence labeled by its start date.
    ss = ShiftSet.parse("day=06:00-18:00,night=18:00-06:00")
    base = dt.datetime(2026, 5, 29, 17, 58, 0, tzinfo=dt.timezone.utc)
    recs = make_minute_records(6, base=base)  # 17:58..18:03 starts
    store = ColumnStore.from_records(recs)
    occ = shift_occurrences(store, ss, tz=None)
    # occurrences are (label, name, lo, hi) contiguous ranges, time-ordered.
    labels = [o[0] for o in occ]
    names = [o[1] for o in occ]
    assert names == ["day", "night"]
    assert labels[1] == "night 2026-05-29"
    # contiguous & covering
    assert occ[0][2] == 0 and occ[-1][3] == store.n
    for a, b in zip(occ, occ[1:]):
        assert a[3] == b[2]


def test_shift_occurrences_midnight_span_is_one_bucket():
    # Build records crossing local midnight inside the night shift. Use UTC tz
    # so wall = stored. 23:58:00 .. 00:02:00 (next day) → all night, ONE
    # occurrence labeled by the START date (the 29th).
    ss = ShiftSet.parse("day=06:00-18:00,night=18:00-06:00")
    base = dt.datetime(2026, 5, 29, 23, 58, 0, tzinfo=dt.timezone.utc)
    recs = make_minute_records(5, base=base)  # 23:58,23:59,00:00,00:01,00:02
    store = ColumnStore.from_records(recs)
    occ = shift_occurrences(store, ss, tz=None)
    night_occ = [o for o in occ if o[1] == "night"]
    assert len(night_occ) == 1
    assert night_occ[0][0] == "night 2026-05-29"
    assert night_occ[0][2] == 0 and night_occ[0][3] == 5


# --- comparison rows (the headline output) -----------------------------------

def test_shift_comparison_rows_schema_and_values():
    # Two shifts, distinct power levels so the comparison is meaningful.
    # Build 4 minutes: 2 min in day (05:59 → no, use 06:00..) and 2 in night.
    # Easier: 17:59:00 (day) ×60 then 18:00:00 (night) ×60, each 1/sec.
    overrides = {}
    for i in range(60):
        overrides[i] = {"P_total_avg_W": 10_000.0}        # day records
    for i in range(60, 120):
        overrides[i] = {"P_total_avg_W": 20_000.0}        # night records
    base = dt.datetime(2026, 5, 29, 17, 59, 0, tzinfo=dt.timezone.utc)
    recs = make_records(120, base=base, overrides=overrides)
    store = ColumnStore.from_records(recs)
    ss = ShiftSet.parse("day=06:00-18:00,night=18:00-06:00")
    rows = shift_comparison_rows(store, ss, events=[], tz=None,
                                 nominal_ln_v=277.0, demand_window=15)
    by = {r["shift"]: r for r in rows}
    assert set(by) == {"day", "night"}
    d, ngt = by["day"], by["night"]
    assert d["records"] == 60
    assert ngt["records"] == 60
    assert d["window"] == "06:00-18:00"
    assert ngt["window"] == "18:00-06:00"
    assert d["P_total_avg_W"] == pytest.approx(10_000.0)
    assert ngt["P_total_avg_W"] == pytest.approx(20_000.0)
    assert ngt["P_total_avg_W"] > d["P_total_avg_W"]
    # energy = mean power * hours; 60 records = 60 s = 1/60 h.
    assert d["kWh"] == pytest.approx(10.0 / 1000.0 * (60 / 3600.0) * 1000)  # 10 kW * (60s/3600) h
    # required schema keys present
    for k in ("shift", "window", "records", "hours", "kWh", "P_total_avg_W",
              "P_total_min_W", "P_total_max_W", "peak_demand_kW", "PF_avg",
              "V_LN_avg_V", "V_LN_p5_V", "V_LN_p95_V", "V_THD_p95_pct",
              "n_outages", "n_dips", "n_swells", "outage_minutes"):
        assert k in d, f"missing schema key {k}"


def test_shift_comparison_multi_day_aggregates_across_occurrences():
    # Two day-shift windows on two different dates aggregate into ONE day row.
    # Day A: 2026-05-29 08:00 ×30 @ 5kW ; Day B: 2026-05-30 08:00 ×30 @ 15kW.
    # Using a 1-day gap of night records between them so they're non-contiguous.
    recs = []
    recs += make_records(30, base=dt.datetime(2026, 5, 29, 8, 0, tzinfo=dt.timezone.utc),
                         defaults={"P_total_avg_W": 5_000.0})
    recs += make_records(30, base=dt.datetime(2026, 5, 29, 20, 0, tzinfo=dt.timezone.utc),
                         defaults={"P_total_avg_W": 1_000.0})  # night
    recs += make_records(30, base=dt.datetime(2026, 5, 30, 8, 0, tzinfo=dt.timezone.utc),
                         defaults={"P_total_avg_W": 15_000.0})
    # Re-index the records so indices are contiguous 0..89 with correct times.
    from fluke_3540.parser import Record
    fixed = [Record(index=i, start=r.start, end=r.end, floats=r.floats)
             for i, r in enumerate(recs)]
    store = ColumnStore.from_records(fixed)
    ss = ShiftSet.parse("day=06:00-18:00,night=18:00-06:00")
    rows = shift_comparison_rows(store, ss, events=[], tz=None,
                                 nominal_ln_v=277.0, demand_window=15)
    by = {r["shift"]: r for r in rows}
    assert by["day"]["records"] == 60       # both day windows merged
    assert by["night"]["records"] == 30
    # day avg power = mean of 30×5k + 30×15k = 10k
    assert by["day"]["P_total_avg_W"] == pytest.approx(10_000.0)
