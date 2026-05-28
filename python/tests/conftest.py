"""Shared pytest fixtures and synthetic-data helpers."""
from __future__ import annotations

import datetime as dt
import struct
import zipfile
from pathlib import Path

import pytest

from fluke_3540.parser import (
    DATA_FLOATS,
    FIELDS,
    FILETIME_EPOCH,
    HEADER_BYTES,
    RECORD_MAGIC,
    RECORD_SIZE,
    Record,
)


# Field-name → float-index lookup for use in tests that build synthetic Records.
FIELD_INDEX = {f.name: f.index for f in FIELDS}


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


@pytest.fixture(scope="session")
def synthetic_fel_path(tmp_path_factory, synthetic_trend_path: Path) -> Path:
    """A synthetic .fel zip-bundle wrapping the synthetic trend.bin in an ES.SYN/ folder."""
    d = tmp_path_factory.mktemp("fel_fixture")
    fel = d / "synthetic.fel"
    with zipfile.ZipFile(fel, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(synthetic_trend_path, arcname="ES.SYN/trend.bin")
        # Throw in a config.json so the fallback glob is exercised too
        zf.writestr("ES.SYN/ES.SYN-config.json",
                    '{"asset_name": "TEST", "team_name": "synthetic", "type": "Fluke3540FC"}')
    return fel


# --- Per-second Record builder for event-detection tests ---------------------

def make_records(
    count: int,
    base: dt.datetime = SYNTHETIC_BASE,
    overrides: dict[int, dict[str, float]] | None = None,
    defaults: dict[str, float] | None = None,
) -> list[Record]:
    """Build a list of in-memory Record objects for tests.

    Every field defaults to whatever's in `defaults` (or 0.0 if absent).
    `overrides[record_index][field_name] = value` injects per-record overrides.

    The defaults mirror a healthy 277 V_LN / 60 Hz / 100A / 50 kW load:
        V_LN_*_{min,max,avg}_V = 277
        V_LL_*_{min,max,avg}_V = 480
        I_*_{min,max,avg}_A    = 100
        freq_*_Hz              = 60.0
        P_total_avg_W          = 50_000
    """
    overrides = overrides or {}
    base_values = {
        **{f.name: 0.0 for f in FIELDS},
        # voltage L-N defaults
        **{f"V_LN_{ph}_{st}_V": 277.0
           for ph in ("a", "b", "c") for st in ("min", "max", "avg")},
        # voltage L-L defaults
        **{f"V_LL_{pair}_{st}_V": 480.0
           for pair in ("ab", "bc", "ca") for st in ("min", "max", "avg")},
        # current defaults
        **{f"I_{ph}_{st}_A": 100.0
           for ph in ("a", "b", "c") for st in ("min", "max", "avg")},
        # frequency
        "freq_min_Hz": 60.0, "freq_max_Hz": 60.0, "freq_avg_Hz": 60.0,
        # power
        "P_total_avg_W": 50_000.0, "P_total_min_W": 50_000.0, "P_total_max_W": 50_000.0,
    }
    if defaults:
        base_values.update(defaults)

    records: list[Record] = []
    for n in range(count):
        per_field = dict(base_values)
        if n in overrides:
            per_field.update(overrides[n])
        floats = [0.0] * DATA_FLOATS
        for name, val in per_field.items():
            if name in FIELD_INDEX:
                floats[FIELD_INDEX[name]] = val
        records.append(Record(
            index=n,
            start=base + dt.timedelta(seconds=n),
            end=base + dt.timedelta(seconds=n + 1),
            floats=tuple(floats),
        ))
    return records


def plant_window(overrides: dict[int, dict[str, float]],
                 start: int, end: int, values: dict[str, float]) -> None:
    """Inject the same field values across records [start, end] inclusive."""
    for i in range(start, end + 1):
        overrides.setdefault(i, {}).update(values)
