"""Shared pytest fixtures — most importantly the synthetic trend.bin builder."""
from __future__ import annotations

import datetime as dt
import struct
from pathlib import Path

import pytest

from fluke_3540.parser import (
    DATA_FLOATS,
    FILETIME_EPOCH,
    HEADER_BYTES,
    RECORD_MAGIC,
    RECORD_SIZE,
)


# 2024-01-13 22:00:00 UTC — convenient round timestamp for the synthetic fixture.
SYNTHETIC_BASE = dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)
SYNTHETIC_RECORD_COUNT = 10


def dt_to_filetime(value: dt.datetime) -> int:
    delta = value - FILETIME_EPOCH
    # FILETIME = 100 ns ticks => 10 ticks per microsecond
    return int(delta.total_seconds() * 10_000_000)


def synthetic_value(record_index: int, field_index: int) -> float:
    """Deterministic, easily-recomputed value at a given (record, field) cell.

    Always positive so reverse-cts sign flips are unambiguous in tests.
    """
    return float((record_index + 1) * 100) + float(field_index) * 0.5


def build_synthetic_trend(path: Path, count: int = SYNTHETIC_RECORD_COUNT) -> None:
    """Write a deterministic trend.bin with `count` records to `path`."""
    with path.open("wb") as fh:
        for n in range(count):
            start_ft = dt_to_filetime(SYNTHETIC_BASE + dt.timedelta(seconds=n))
            end_ft = dt_to_filetime(SYNTHETIC_BASE + dt.timedelta(seconds=n + 1))
            header = (
                RECORD_MAGIC
                + struct.pack("<II", start_ft >> 32 & 0xFFFFFFFF, start_ft & 0xFFFFFFFF)
                + struct.pack("<II", end_ft >> 32 & 0xFFFFFFFF, end_ft & 0xFFFFFFFF)
                + struct.pack("<I", 0)  # count_or_reserved
            )
            assert len(header) == HEADER_BYTES
            floats = struct.pack(
                f"<{DATA_FLOATS}f",
                *(synthetic_value(n, i) for i in range(DATA_FLOATS)),
            )
            fh.write(header + floats)
            assert len(header) + len(floats) == RECORD_SIZE


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    d = Path(__file__).parent / "fixtures"
    d.mkdir(exist_ok=True)
    return d


@pytest.fixture(scope="session")
def synthetic_trend_path(fixtures_dir: Path) -> Path:
    """Path to a freshly-built synthetic trend.bin in the fixtures dir."""
    p = fixtures_dir / "synthetic_trend.bin"
    build_synthetic_trend(p)
    return p


@pytest.fixture(scope="session")
def synthetic_session_dir(tmp_path_factory, synthetic_trend_path: Path) -> Path:
    """A fake ES.SYN/ session directory containing just trend.bin (the only required file)."""
    d = tmp_path_factory.mktemp("ES.SYN")
    (d / "trend.bin").write_bytes(synthetic_trend_path.read_bytes())
    return d
