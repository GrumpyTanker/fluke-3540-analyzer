"""Tests for clock-correction anchors (read_first_last_times / compute_time_shift)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from fluke_3540.parser import compute_time_shift, read_first_last_times

from conftest import build_large_trend, SYNTHETIC_BASE


def _trend(tmp_path: Path, count: int) -> Path:
    p = tmp_path / "trend.bin"
    build_large_trend(p, count=count)
    return p


def test_read_first_last(tmp_path: Path):
    p = _trend(tmp_path, 100)
    first, last = read_first_last_times(p)
    assert first == SYNTHETIC_BASE
    assert last == SYNTHETIC_BASE + dt.timedelta(seconds=100)


def test_anchor_end_shift(tmp_path: Path):
    p = _trend(tmp_path, 100)  # last end = base + 100s
    real_end = dt.datetime(2026, 6, 2, 20, 45, 0, tzinfo=dt.timezone.utc)
    shift = compute_time_shift(p, anchor_end=real_end)
    last = SYNTHETIC_BASE + dt.timedelta(seconds=100)
    assert shift == real_end - last
    # applying shift maps last end exactly onto the anchor
    assert last + shift == real_end


def test_anchor_start_shift(tmp_path: Path):
    p = _trend(tmp_path, 100)
    real_start = dt.datetime(2026, 5, 27, 0, 53, 0, tzinfo=dt.timezone.utc)
    shift = compute_time_shift(p, anchor_start=real_start)
    assert SYNTHETIC_BASE + shift == real_start


def test_anchors_mutually_exclusive(tmp_path: Path):
    p = _trend(tmp_path, 10)
    with pytest.raises(ValueError, match="mutually exclusive"):
        compute_time_shift(p, anchor_start=SYNTHETIC_BASE, anchor_end=SYNTHETIC_BASE)


def test_no_anchor_is_zero(tmp_path: Path):
    p = _trend(tmp_path, 10)
    assert compute_time_shift(p) == dt.timedelta(0)
