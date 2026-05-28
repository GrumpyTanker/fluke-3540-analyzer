"""Tests for the CSV-input replacement parser."""
from __future__ import annotations

from pathlib import Path

import pytest

from fluke_3540.parser import DATA_FLOATS, FIELDS, Record, export_csv, from_csv


def test_from_csv_roundtrips_via_export(
    synthetic_session_dir: Path, tmp_path: Path,
):
    """export_csv → from_csv → matches the original records cell-for-cell."""
    csv_path = tmp_path / "session.csv"
    export_csv(synthetic_session_dir, csv_path)
    records = list(from_csv(csv_path))
    assert len(records) == 10
    # Spot-check several cells against the deterministic formula
    from conftest import synthetic_value
    for n in (0, 5, 9):
        for i in (0, 47, 100, 179):
            assert records[n].floats[i] == pytest.approx(synthetic_value(n, i))


def test_from_csv_records_have_correct_timestamps(
    synthetic_session_dir: Path, tmp_path: Path,
):
    import datetime as dt
    csv_path = tmp_path / "session.csv"
    export_csv(synthetic_session_dir, csv_path)
    records = list(from_csv(csv_path))
    base = dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)
    for n, r in enumerate(records):
        assert r.start == base + dt.timedelta(seconds=n)
        assert r.end == base + dt.timedelta(seconds=n + 1)


def test_from_csv_handles_missing_columns(tmp_path: Path):
    """A CSV with only a subset of fields populates the others as 0.0."""
    csv_path = tmp_path / "partial.csv"
    csv_path.write_text(
        "timestamp_utc,window_end_utc,P_total_avg_W,V_LN_a_avg_V\n"
        "2024-01-13T22:00:00+00:00,2024-01-13T22:00:01+00:00,12345,277.5\n",
        encoding="utf-8",
    )
    records = list(from_csv(csv_path))
    assert len(records) == 1
    by_name = {f.name: f.index for f in FIELDS}
    assert records[0].floats[by_name["P_total_avg_W"]] == 12345
    assert records[0].floats[by_name["V_LN_a_avg_V"]] == 277.5
    # Untouched fields remain 0.0
    assert records[0].floats[by_name["V_LN_b_avg_V"]] == 0.0


def test_from_csv_ignores_empty_cells_gracefully(tmp_path: Path):
    csv_path = tmp_path / "with_blanks.csv"
    csv_path.write_text(
        "timestamp_utc,window_end_utc,P_total_avg_W,V_LN_a_avg_V\n"
        "2024-01-13T22:00:00+00:00,2024-01-13T22:00:01+00:00,,277\n"
        "2024-01-13T22:00:01+00:00,2024-01-13T22:00:02+00:00,not_a_number,278\n",
        encoding="utf-8",
    )
    records = list(from_csv(csv_path))
    assert len(records) == 2
    by_name = {f.name: f.index for f in FIELDS}
    assert records[0].floats[by_name["P_total_avg_W"]] == 0.0  # empty
    assert records[1].floats[by_name["P_total_avg_W"]] == 0.0  # unparseable
    assert records[0].floats[by_name["V_LN_a_avg_V"]] == 277
    assert records[1].floats[by_name["V_LN_a_avg_V"]] == 278


def test_from_csv_handles_naive_timestamps(tmp_path: Path):
    """Timestamps without a timezone offset should be treated as UTC."""
    csv_path = tmp_path / "naive.csv"
    csv_path.write_text(
        "timestamp_utc,window_end_utc,P_total_avg_W\n"
        "2024-01-13T22:00:00,2024-01-13T22:00:01,1000\n",
        encoding="utf-8",
    )
    records = list(from_csv(csv_path))
    assert len(records) == 1
    assert records[0].start.tzinfo is not None


def test_cli_main_accepts_csv_input(synthetic_session_dir: Path, tmp_path: Path):
    """End-to-end: CLI --parse-only on a CSV produces the standard artifacts."""
    from fluke_3540.cli import main
    csv_path = tmp_path / "in.csv"
    export_csv(synthetic_session_dir, csv_path)
    out = tmp_path / "out"
    rc = main([str(csv_path), "-o", str(out), "--parse-only"])
    assert rc == 0
    assert (out / "session.csv").is_file()
    assert (out / "session_1min.csv").is_file()
    assert (out / "events.json").is_file()
    assert (out / "snapshots.json").is_file()
    assert (out / "insights.json").is_file()
