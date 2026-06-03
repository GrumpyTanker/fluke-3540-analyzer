"""Tests for the XLSX report — Statistics and Time-of-Day sheets."""
from __future__ import annotations

from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

from fluke_3540.analysis import time_of_day_profile, whole_session_stats
from fluke_3540.plots.xlsx import write_xlsx
from fluke_3540.store import ColumnStore

from conftest import make_records


def _write_min_csv(path: Path) -> None:
    # write_xlsx reads a CSV with the field-map columns it knows about.
    import datetime as dt
    cols = ["timestamp_utc", "window_end_utc", "P_total_avg_W", "S_total_avg_VA",
            "Q_total_avg_VAR", "PF_total_avg", "V_LN_a_avg_V", "V_LN_b_avg_V",
            "V_LN_c_avg_V", "I_a_avg_A", "I_b_avg_A", "I_c_avg_A",
            "freq_avg_Hz", "Wh_total"]
    base = dt.datetime(2024, 1, 13, 22, 0, 0)
    with path.open("w", newline="", encoding="utf-8") as fh:
        import csv
        w = csv.writer(fh)
        w.writerow(cols)
        for i in range(5):
            t = base + dt.timedelta(minutes=i)
            w.writerow([t.isoformat(), (t).isoformat(), 50000, 52000, 1000, 0.96,
                        277, 277, 277, 100, 100, 100, 60.0, 13.9])


def test_xlsx_has_stats_and_tod_sheets(tmp_path: Path):
    csv_path = tmp_path / "min.csv"
    _write_min_csv(csv_path)
    store = ColumnStore.from_records(make_records(600))
    stats = whole_session_stats(store)
    tod = time_of_day_profile(store, window=(0, 1440), bin_minutes=1)
    out = tmp_path / "report.xlsx"
    write_xlsx(csv_path, out, config={"asset_name": "TEST"},
               csv_per_second_path=csv_path, stats=stats, tod_rows=tod)
    wb = openpyxl.load_workbook(out)
    assert "Statistics" in wb.sheetnames
    assert "Time of Day" in wb.sheetnames


def test_xlsx_without_extras_still_works(tmp_path: Path):
    csv_path = tmp_path / "min.csv"
    _write_min_csv(csv_path)
    out = tmp_path / "report.xlsx"
    write_xlsx(csv_path, out, config={"asset_name": "TEST"})
    wb = openpyxl.load_workbook(out)
    assert "Summary" in wb.sheetnames
    assert "Statistics" not in wb.sheetnames
