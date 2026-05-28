"""Tests for the binary parser — synthetic fixture + spec-driven assertions."""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import pytest

from fluke_3540.parser import (
    DATA_FLOATS,
    FIELDS,
    RECORD_MAGIC,
    RECORD_SIZE,
    SPEC,
    export_csv,
    filetime_to_dt,
    iter_records,
    open_session,
    reverse_cts_indices,
)

from conftest import (
    SYNTHETIC_BASE,
    SYNTHETIC_RECORD_COUNT,
    synthetic_value,
)


# --- Spec sanity --------------------------------------------------------------

def test_spec_constants_match_known_layout():
    assert SPEC["record_size"] == 744
    assert SPEC["header_bytes"] == 24
    assert SPEC["data_floats"] == 180
    assert RECORD_SIZE == 744
    assert DATA_FLOATS == 180
    assert RECORD_MAGIC == b"\x46\x00\xe8\x02"


def test_field_map_has_no_duplicate_indices():
    indices = [f.index for f in FIELDS]
    assert len(indices) == len(set(indices))


def test_field_map_all_in_range():
    assert all(0 <= f.index < DATA_FLOATS for f in FIELDS)


def test_specific_known_field_names():
    by_index = {f.index: f.name for f in FIELDS}
    assert by_index[0] == "V_LN_a_min_V"
    assert by_index[18] == "I_a_min_A"
    assert by_index[45] == "freq_min_Hz"
    assert by_index[57] == "P_total_min_W"
    assert by_index[144] == "Wh_a"
    assert by_index[147] == "Wh_total"


# --- iter_records -------------------------------------------------------------

def test_iter_records_count(synthetic_trend_path: Path):
    records = list(iter_records(synthetic_trend_path))
    assert len(records) == SYNTHETIC_RECORD_COUNT


def test_iter_records_timestamps(synthetic_trend_path: Path):
    records = list(iter_records(synthetic_trend_path))
    for n, rec in enumerate(records):
        expected_start = SYNTHETIC_BASE + dt.timedelta(seconds=n)
        expected_end = SYNTHETIC_BASE + dt.timedelta(seconds=n + 1)
        assert rec.start == expected_start
        assert rec.end == expected_end


def test_iter_records_float_values(synthetic_trend_path: Path):
    records = list(iter_records(synthetic_trend_path))
    # spot-check several cells against the deterministic formula
    assert records[0].floats[0] == pytest.approx(synthetic_value(0, 0))
    assert records[0].floats[47] == pytest.approx(synthetic_value(0, 47))
    assert records[5].floats[100] == pytest.approx(synthetic_value(5, 100))
    assert records[9].floats[179] == pytest.approx(synthetic_value(9, 179))


def test_iter_records_bad_magic_raises(tmp_path: Path):
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"\x00" * RECORD_SIZE)
    with pytest.raises(ValueError, match="Bad magic"):
        list(iter_records(bad))


# --- filetime_to_dt -----------------------------------------------------------

def test_filetime_to_dt_epoch_is_1601():
    assert filetime_to_dt(0) == dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)


def test_filetime_to_dt_known_value():
    # 2024-01-13 22:00:00 UTC = (2024-01-13 - 1601-01-01) in 100ns ticks
    delta = dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc) - \
        dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
    ticks = int(delta.total_seconds() * 10_000_000)
    assert filetime_to_dt(ticks) == \
        dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)


# --- export_csv ---------------------------------------------------------------

def test_export_csv_writes_correct_row_and_column_count(
    synthetic_session_dir: Path, tmp_path: Path,
):
    out = tmp_path / "out.csv"
    result = export_csv(synthetic_session_dir, out)
    assert result["rows_written"] == SYNTHETIC_RECORD_COUNT
    # 3 fixed cols (record_index, timestamp_utc, window_end_utc) + len(FIELDS)
    assert result["columns"] == 3 + len(FIELDS)

    with out.open() as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == SYNTHETIC_RECORD_COUNT + 1  # +1 for header
    assert rows[0][:3] == ["record_index", "timestamp_utc", "window_end_utc"]


def test_export_csv_every_n(synthetic_session_dir: Path, tmp_path: Path):
    out = tmp_path / "out.csv"
    result = export_csv(synthetic_session_dir, out, every=2)
    # records 0, 2, 4, 6, 8
    assert result["rows_written"] == 5


def test_export_csv_limit(synthetic_session_dir: Path, tmp_path: Path):
    out = tmp_path / "out.csv"
    result = export_csv(synthetic_session_dir, out, limit=3)
    assert result["rows_written"] == 3


# --- reverse_cts --------------------------------------------------------------

def test_reverse_cts_indices_includes_p_and_q(synthetic_session_dir: Path):
    rev = reverse_cts_indices()
    by_name = {f.name: f.index for f in FIELDS}
    # Should be flipped:
    for name in ("P_a_avg_W", "Q_a_avg_VAR", "PF_total_avg",
                 "DPF_a_avg", "Wh_a", "VARh_total", "P1_a_avg_W", "Q1_total_avg_VAR"):
        assert by_name[name] in rev, f"{name} should be in reverse-cts set"


def test_reverse_cts_indices_excludes_voltage_current(synthetic_session_dir: Path):
    rev = reverse_cts_indices()
    by_name = {f.name: f.index for f in FIELDS}
    # Should NOT be flipped:
    for name in ("V_LN_a_avg_V", "V_LL_ab_avg_V", "I_a_avg_A",
                 "S_a_avg_VA", "S_total_avg_VA", "freq_avg_Hz",
                 "V_THD_pct_a_avg", "I_THD_pct_a_avg", "VAh_a"):
        assert by_name[name] not in rev, f"{name} should NOT be in reverse-cts set"


# --- .fel zip-bundle support (F1) -------------------------------------------

def test_open_session_yields_directory_unchanged(synthetic_session_dir: Path):
    with open_session(synthetic_session_dir) as session_dir:
        assert session_dir == synthetic_session_dir


def test_open_session_unpacks_fel(synthetic_fel_path: Path):
    with open_session(synthetic_fel_path) as session_dir:
        assert session_dir.is_dir()
        assert (session_dir / "trend.bin").is_file()
        records = list(iter_records(session_dir / "trend.bin"))
        assert len(records) == SYNTHETIC_RECORD_COUNT


def test_export_csv_via_fel_matches_direct_parse(
    synthetic_fel_path: Path, synthetic_session_dir: Path, tmp_path: Path,
):
    from fluke_3540.parser import find_session_files
    fel_out = tmp_path / "via_fel.csv"
    direct_out = tmp_path / "via_dir.csv"
    with open_session(synthetic_fel_path) as session_dir:
        export_csv(session_dir, fel_out)
    export_csv(synthetic_session_dir, direct_out)
    assert fel_out.read_text() == direct_out.read_text()


def test_open_session_rejects_unknown_file_type(tmp_path: Path):
    bad = tmp_path / "not_a_session.txt"
    bad.write_text("hello")
    with pytest.raises(ValueError, match="ES.NNN/ directory or a .fel"):
        with open_session(bad):
            pass


def test_open_session_rejects_missing_path(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        with open_session(tmp_path / "does_not_exist"):
            pass


def test_find_session_files_globs_config_json_fallback(
    synthetic_fel_path: Path,
):
    """The .fel unpacks into ES.SYN/ — config json filename should still be picked up."""
    from fluke_3540.parser import find_session_files
    with open_session(synthetic_fel_path) as session_dir:
        files = find_session_files(session_dir)
        assert files["trend"].is_file()
        # config_json should glob-match ES.SYN-config.json
        assert files["config_json"].is_file()


# Need SYNTHETIC_RECORD_COUNT for the .fel test
from conftest import SYNTHETIC_RECORD_COUNT  # noqa: E402


def test_export_csv_reverse_cts_negates_correct_columns(
    synthetic_session_dir: Path, tmp_path: Path,
):
    out_plain = tmp_path / "plain.csv"
    out_flipped = tmp_path / "flipped.csv"
    export_csv(synthetic_session_dir, out_plain, reverse_cts=False)
    export_csv(synthetic_session_dir, out_flipped, reverse_cts=True)

    with out_plain.open() as fh:
        plain_rows = list(csv.reader(fh))
    with out_flipped.open() as fh:
        flipped_rows = list(csv.reader(fh))

    headers = plain_rows[0]
    flip_set = reverse_cts_indices()
    by_name = {f.name: f.index for f in FIELDS}

    # check row 0 (record 0) for every field column
    for col_idx, col_name in enumerate(headers[3:], start=3):
        plain_val = float(plain_rows[1][col_idx])
        flipped_val = float(flipped_rows[1][col_idx])
        if by_name[col_name] in flip_set:
            assert flipped_val == pytest.approx(-plain_val), \
                f"{col_name} should be negated but isn't"
        else:
            assert flipped_val == pytest.approx(plain_val), \
                f"{col_name} should NOT be negated but was"
