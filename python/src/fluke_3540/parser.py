"""Parse a Fluke 3540 FC session directory (ES.NNN/) into a labeled CSV.

Binary layout and field map are loaded from ``spec/field_map.json`` at the
repo root — the same file the JavaScript port uses, so both implementations
stay in lockstep.

Usage:
    python -m fluke_3540.parser path/to/ES.NNN [--output out.csv] [--limit N] [--every K]

One row per record (1 record/second on the 3540 FC). See spec/field_map.json
for the full column list.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


# --- Spec loading -------------------------------------------------------------

def _find_field_map() -> Path:
    """Locate spec/field_map.json by walking up from this module."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "spec" / "field_map.json"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Could not locate spec/field_map.json. Expected it at the repo root."
    )


@dataclass(frozen=True)
class FieldSpec:
    index: int
    name: str
    confidence: str  # H / M / L / ?


def _load_spec(path: Path | None = None) -> tuple[dict, list[FieldSpec], frozenset[int]]:
    spec_path = path or _find_field_map()
    raw = json.loads(spec_path.read_text())
    fields = [
        FieldSpec(f["index"], f["name"], f["confidence"]) for f in raw["fields"]
    ]
    seen = [f.index for f in fields]
    if len(seen) != len(set(seen)):
        dupes = sorted({i for i in seen if seen.count(i) > 1})
        raise RuntimeError(f"spec has duplicate field indices: {dupes}")
    if any(not (0 <= f.index < raw["data_floats"]) for f in fields):
        raise RuntimeError("spec has out-of-range field indices")
    reverse_prefixes = tuple(raw["reverse_cts_prefixes"])
    reverse_indices = frozenset(
        f.index for f in fields if f.name.startswith(reverse_prefixes)
    )
    return raw, fields, reverse_indices


SPEC, FIELDS, _REVERSE_CTS_INDICES = _load_spec()

RECORD_MAGIC = bytes(SPEC["record_magic"])
RECORD_SIZE = SPEC["record_size"]
HEADER_BYTES = SPEC["header_bytes"]
DATA_FLOATS = SPEC["data_floats"]
FILETIME_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)


def reverse_cts_indices() -> frozenset[int]:
    """Set of field indices whose sign should be flipped when --reverse-cts is set.

    Covers P/P1/Q/Q1/PF/DPF/Wh/VARh — the signed quantities that depend on
    iFlex CT probe orientation. I/S/VA/V/THD/freq are sign-independent.
    """
    return _REVERSE_CTS_INDICES


# --- Parsing ------------------------------------------------------------------

@dataclass(frozen=True)
class Record:
    index: int
    start: dt.datetime
    end: dt.datetime
    floats: tuple[float, ...]


def filetime_to_dt(value: int) -> dt.datetime:
    """Convert a Windows FILETIME (100 ns ticks since 1601-01-01 UTC) to datetime."""
    return FILETIME_EPOCH + dt.timedelta(microseconds=value / 10)


def iter_records(path: Path) -> Iterator[Record]:
    """Yield decoded Record objects from a trend.bin file."""
    with path.open("rb") as fh:
        index = 0
        while True:
            chunk = fh.read(RECORD_SIZE)
            if len(chunk) < RECORD_SIZE:
                return
            if chunk[:4] != RECORD_MAGIC:
                raise ValueError(
                    f"Bad magic at record {index} (offset 0x{index * RECORD_SIZE:x})"
                )
            start_hi, start_lo = struct.unpack_from("<II", chunk, 4)
            end_hi, end_lo = struct.unpack_from("<II", chunk, 12)
            start_ft = (start_hi << 32) | start_lo
            end_ft = (end_hi << 32) | end_lo
            floats = struct.unpack_from(f"<{DATA_FLOATS}f", chunk, HEADER_BYTES)
            yield Record(
                index=index,
                start=filetime_to_dt(start_ft),
                end=filetime_to_dt(end_ft),
                floats=floats,
            )
            index += 1


def find_session_files(session_dir: Path) -> dict:
    """Locate the four well-known files inside an ES.NNN/ directory."""
    name = session_dir.name  # "ES.002"
    files = {
        "config_json":  session_dir / f"{name}-config.json",
        "trend":        session_dir / "trend.bin",
        "trend_meta":   session_dir / "trend-meta.bin",
        "session_meta": session_dir / "session-meta.bin",
        "config":       session_dir / "configuration.bin",
    }
    if not files["trend"].exists():
        raise FileNotFoundError(f"Required trend.bin not found in {session_dir}")
    return files


def export_csv(session_dir: Path, output: Path, limit: int | None = None,
               every: int = 1, reverse_cts: bool = False) -> dict:
    files = find_session_files(session_dir)
    config = {}
    if files["config_json"].exists():
        config = json.loads(files["config_json"].read_text())

    headers = ["record_index", "timestamp_utc", "window_end_utc"]
    headers += [f.name for f in FIELDS]
    flip = reverse_cts_indices() if reverse_cts else frozenset()

    written = 0
    first_ts = None
    last_ts = None
    with output.open("w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(headers)
        for rec in iter_records(files["trend"]):
            if rec.index % every != 0:
                continue
            if limit is not None and written >= limit:
                break
            if first_ts is None:
                first_ts = rec.start
            last_ts = rec.end
            row = [rec.index, rec.start.isoformat(), rec.end.isoformat()]
            for f in FIELDS:
                v = rec.floats[f.index]
                if f.index in flip:
                    v = -v
                row.append(v)
            writer.writerow(row)
            written += 1

    return {
        "config": config,
        "rows_written": written,
        "first_ts": first_ts.isoformat() if first_ts else None,
        "last_ts": last_ts.isoformat() if last_ts else None,
        "columns": len(headers),
        "reverse_cts": reverse_cts,
        "reversed_columns": sorted(f.name for f in FIELDS if f.index in flip),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("session_dir", type=Path, help="Path to ES.NNN session directory")
    ap.add_argument("--output", "-o", type=Path, default=None,
                    help="Output CSV path (default: <session>.csv next to the dir)")
    ap.add_argument("--limit", type=int, default=None, help="Cap output rows")
    ap.add_argument("--every", type=int, default=1,
                    help="Emit every N-th record (1 = all). Use 60 for 1 row/min.")
    ap.add_argument("--reverse-cts", action="store_true",
                    help="Negate P/P1/Q/Q1/PF/DPF/Wh/VARh to correct for physically "
                         "reversed iFlex CT probes. I/S/VA/V columns are unchanged.")
    args = ap.parse_args(argv)

    if not args.session_dir.is_dir():
        print(f"ERROR: {args.session_dir} is not a directory", file=sys.stderr)
        return 1
    output = args.output or args.session_dir.with_suffix(".csv")
    result = export_csv(
        args.session_dir, output,
        limit=args.limit, every=args.every, reverse_cts=args.reverse_cts,
    )
    print(f"Wrote {output}")
    print(f"  rows: {result['rows_written']}")
    print(f"  columns: {result['columns']}")
    print(f"  time range (UTC): {result['first_ts']} .. {result['last_ts']}")
    if result["reverse_cts"]:
        print(f"  --reverse-cts: negated {len(result['reversed_columns'])} columns "
              "(P*/Q*/PF*/DPF*/Wh*/VARh*)")
    cfg = result["config"]
    if cfg:
        print(f"  asset: {cfg.get('asset_name')}  team: {cfg.get('team_name')}")
        print(f"  instrument: {cfg.get('type')}  fw={cfg.get('firmware_version')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
