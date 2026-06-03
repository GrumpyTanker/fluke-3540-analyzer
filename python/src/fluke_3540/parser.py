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
import contextlib
import csv
import datetime as dt
import json
import struct
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


# --- Spec loading -------------------------------------------------------------

def _find_field_map() -> Path:
    """Locate spec/field_map.json.

    Looks first at a sibling ``spec/`` next to this module — the layout
    produced by the release workflow when bundling the spec into the
    distributed wheel. Falls back to walking up from this file to find
    ``<repo>/spec/field_map.json``, which is what editable installs and
    source checkouts have.
    """
    here = Path(__file__).resolve()
    # 1) Installed-wheel layout: site-packages/fluke_3540/spec/field_map.json
    bundled = here.parent / "spec" / "field_map.json"
    if bundled.is_file():
        return bundled
    # 2) Source / editable layout: walk up to find repo-root spec/
    for parent in here.parents:
        candidate = parent / "spec" / "field_map.json"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Could not locate spec/field_map.json. Expected it bundled in the "
        "package or at the repo root."
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


SPEC, FIELDS, _REVERSE_CTS_INDICES_ALL = _load_spec()

RECORD_MAGIC = bytes(SPEC["record_magic"])
RECORD_SIZE = SPEC["record_size"]
HEADER_BYTES = SPEC["header_bytes"]
DATA_FLOATS = SPEC["data_floats"]
FILETIME_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)


_VALID_PHASES = frozenset({"a", "b", "c"})


def _field_phase(name: str) -> str | None:
    """Return the phase letter for a reverse-CTS-eligible field, or None.

    Reverse-CTS-eligible names look like ``P_a_avg_W``, ``Q1_total_avg_VAR``,
    ``Wh_b``, etc. — the second underscore-delimited token is the phase
    (``a``/``b``/``c``/``total``). For any field whose name doesn't start
    with a reverse-CTS prefix, returns None.
    """
    if not name.startswith(tuple(SPEC["reverse_cts_prefixes"])):
        return None
    parts = name.split("_")
    if len(parts) < 2:
        return None
    return parts[1]


def reverse_cts_indices(phases: bool | Iterable[str] | None = True) -> frozenset[int]:
    """Field indices whose sign should be flipped to correct reversed iFlex CTs.

    Args:
        phases: ``True`` (default) flips every reverse-CTS-eligible column on
                every phase, matching the original all-or-nothing flag.
                An iterable subset of ``{"a", "b", "c"}`` flips just those
                phases (plus the matching ``*_total_*`` columns, since the
                total includes the flipped phase). ``False`` / ``None`` /
                empty iterable returns an empty set.

    Covers P/P1/Q/Q1/PF/DPF/Wh/VARh — the signed quantities that depend
    on probe orientation. I/S/VA/V/THD/freq are sign-independent.
    """
    if phases is True:
        return _REVERSE_CTS_INDICES_ALL
    if not phases:
        return frozenset()
    phase_set = {p.strip().lower() for p in phases}
    bad = phase_set - _VALID_PHASES
    if bad:
        raise ValueError(
            f"Invalid reverse-CTS phases: {sorted(bad)}. Must be subset of {sorted(_VALID_PHASES)}"
        )
    if not phase_set:
        return frozenset()
    indices: set[int] = set()
    for f in FIELDS:
        ph = _field_phase(f.name)
        if ph is None:
            continue
        if ph in phase_set:
            indices.add(f.index)
        elif ph == "total":
            # totals are sums of phase values — flipping any one phase
            # changes the total, so flip it too.
            indices.add(f.index)
    return frozenset(indices)


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


@dataclass
class ParseStats:
    """Counters from a tolerant parse pass."""
    total_seen: int = 0          # record slots examined (good + bad)
    good: int = 0                # records yielded
    bad_magic: int = 0           # wrong magic — skipped
    truncated: int = 0           # short final chunk
    nonfinite: int = 0           # records with NaN/Inf floats (still yielded)


def iter_records_safe(
    path: Path,
    stats: "ParseStats | None" = None,
    log=None,
    resync: bool = True,
) -> Iterator[Record]:
    """Tolerant variant of :func:`iter_records`.

    Real meter dumps occasionally contain a corrupt record, a garbage tail, or
    non-finite floats. Rather than abort the whole session, this:

    - on a short final chunk: stops cleanly (counts it as ``truncated``);
    - on bad magic: if ``resync`` is True, scans forward byte-by-byte for the
      next valid magic and continues (counts skipped bytes' worth as
      ``bad_magic``); if False, skips the single record slot;
    - flags (but still yields) records containing NaN/Inf floats.

    Pass a :class:`ParseStats` to collect counters and an optional ``log``
    callable (e.g. ``print``) to emit a one-line note per anomaly.
    """
    st = stats if stats is not None else ParseStats()
    index = 0
    with path.open("rb") as fh:
        data_fmt = f"<{DATA_FLOATS}f"
        while True:
            offset = fh.tell()
            chunk = fh.read(RECORD_SIZE)
            if len(chunk) < RECORD_SIZE:
                if chunk:
                    st.truncated += 1
                    if log:
                        log(f"[parse] truncated tail: {len(chunk)} bytes at "
                            f"offset 0x{offset:x} (discarded)")
                return
            st.total_seen += 1
            if chunk[:4] != RECORD_MAGIC:
                st.bad_magic += 1
                if log:
                    log(f"[parse] bad magic at offset 0x{offset:x} — "
                        f"{'resyncing' if resync else 'skipping record'}")
                if resync:
                    # Step forward one byte and search for the next magic.
                    fh.seek(offset + 1)
                continue
            start_hi, start_lo = struct.unpack_from("<II", chunk, 4)
            end_hi, end_lo = struct.unpack_from("<II", chunk, 12)
            start_ft = (start_hi << 32) | start_lo
            end_ft = (end_hi << 32) | end_lo
            floats = struct.unpack_from(data_fmt, chunk, HEADER_BYTES)
            if any(f != f or f in (float("inf"), float("-inf")) for f in floats):
                st.nonfinite += 1
                if log:
                    log(f"[parse] non-finite float(s) in record {index} "
                        f"(offset 0x{offset:x}) — kept, treat with care")
            yield Record(
                index=index,
                start=filetime_to_dt(start_ft),
                end=filetime_to_dt(end_ft),
                floats=floats,
            )
            index += 1
            st.good += 1


def read_first_last_times(trend_path: Path) -> tuple[dt.datetime | None, dt.datetime | None]:
    """Read just the first record's start and the last record's end (no full parse).

    Seeks directly to the final aligned record, so this is O(1) regardless of
    session length — used to compute ``--anchor-*`` time shifts cheaply.
    """
    size = trend_path.stat().st_size
    n = size // RECORD_SIZE
    if n == 0:
        return None, None
    with trend_path.open("rb") as fh:
        head = fh.read(RECORD_SIZE)
        if len(head) < RECORD_SIZE or head[:4] != RECORD_MAGIC:
            first = None
        else:
            s_hi, s_lo = struct.unpack_from("<II", head, 4)
            first = filetime_to_dt((s_hi << 32) | s_lo)
        fh.seek((n - 1) * RECORD_SIZE)
        tail = fh.read(RECORD_SIZE)
        if len(tail) < RECORD_SIZE or tail[:4] != RECORD_MAGIC:
            last = None
        else:
            e_hi, e_lo = struct.unpack_from("<II", tail, 12)
            last = filetime_to_dt((e_hi << 32) | e_lo)
    return first, last


def compute_time_shift(
    trend_path: Path,
    anchor_start: dt.datetime | None = None,
    anchor_end: dt.datetime | None = None,
) -> dt.timedelta:
    """Time shift mapping the meter's wrong RTC to a known real timestamp.

    ``anchor_start`` pins the real start; ``anchor_end`` pins the real end.
    They are mutually exclusive. Returns timedelta(0) if neither is given.
    """
    if anchor_start is not None and anchor_end is not None:
        raise ValueError("anchor_start and anchor_end are mutually exclusive")
    if anchor_start is None and anchor_end is None:
        return dt.timedelta(0)
    first, last = read_first_last_times(trend_path)
    if anchor_start is not None:
        if first is None:
            raise ValueError("Cannot read first record time to anchor start")
        return anchor_start - first
    if last is None:
        raise ValueError("Cannot read last record time to anchor end")
    return anchor_end - last


def from_csv(path: Path) -> Iterator[Record]:
    """Yield Record objects reconstructed from a previously-exported session CSV.

    The CSV must have ``timestamp_utc`` (ISO-8601), ``window_end_utc``, and
    any subset of the field-map column names. Missing or unparseable cells
    become 0.0. Useful for re-analysing a session whose binary you no longer
    have, or for sharing a parsed session as a single text file.
    """
    name_to_idx = {f.name: f.index for f in FIELDS}
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for n, row in enumerate(reader):
            try:
                start = dt.datetime.fromisoformat(row["timestamp_utc"])
            except (KeyError, ValueError):
                continue
            try:
                end = dt.datetime.fromisoformat(row["window_end_utc"])
            except (KeyError, ValueError):
                end = start + dt.timedelta(seconds=1)
            if start.tzinfo is None:
                start = start.replace(tzinfo=dt.timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=dt.timezone.utc)
            floats = [0.0] * DATA_FLOATS
            for col, val in row.items():
                idx = name_to_idx.get(col)
                if idx is None or val == "":
                    continue
                try:
                    floats[idx] = float(val)
                except ValueError:
                    pass
            yield Record(
                index=n,
                start=start,
                end=end,
                floats=tuple(floats),
            )


def find_session_files(session_dir: Path) -> dict:
    """Locate the well-known files inside an ES.NNN/ directory.

    The config-json filename usually matches the directory name (e.g.
    ES.002-config.json), but for sessions unpacked from a .fel the
    parent directory name on disk may not match — we glob for *-config.json
    as a fallback.
    """
    name = session_dir.name
    config_json = session_dir / f"{name}-config.json"
    if not config_json.exists():
        matches = list(session_dir.glob("*-config.json"))
        config_json = matches[0] if matches else config_json
    files = {
        "config_json":  config_json,
        "trend":        session_dir / "trend.bin",
        "trend_meta":   session_dir / "trend-meta.bin",
        "session_meta": session_dir / "session-meta.bin",
        "config":       session_dir / "configuration.bin",
    }
    if not files["trend"].exists():
        raise FileNotFoundError(f"Required trend.bin not found in {session_dir}")
    return files


@contextlib.contextmanager
def open_session(path: Path) -> Iterator[Path]:
    """Yield a session directory path, transparently unpacking a .fel zip if needed.

    Usage:
        with open_session(Path("session.fel")) as session_dir:
            records = list(iter_records(session_dir / "trend.bin"))
    """
    if path.is_dir():
        yield path
        return
    if not path.is_file():
        raise FileNotFoundError(f"Session input does not exist: {path}")
    if path.suffix.lower() != ".fel":
        raise ValueError(
            f"Session input must be an ES.NNN/ directory or a .fel zip-bundle, got: {path}"
        )
    with tempfile.TemporaryDirectory(prefix="fluke_fel_") as tmpname:
        tmp = Path(tmpname)
        with zipfile.ZipFile(path) as zf:
            zf.extractall(tmp)
        # The .fel typically contains a single ES.NNN/ folder; some have
        # trend.bin at the root instead.
        entries = list(tmp.iterdir())
        if (tmp / "trend.bin").is_file():
            yield tmp
        else:
            dirs = [e for e in entries if e.is_dir()]
            if len(dirs) == 1:
                yield dirs[0]
            elif len(dirs) > 1:
                # Multiple folders — pick the one with trend.bin
                with_trend = [d for d in dirs if (d / "trend.bin").is_file()]
                if len(with_trend) == 1:
                    yield with_trend[0]
                else:
                    raise FileNotFoundError(
                        f"{path}: cannot locate a single session folder inside the .fel"
                    )
            else:
                raise FileNotFoundError(
                    f"{path}: no session folder or trend.bin inside the .fel"
                )


def export_csv(session_dir: Path, output: Path, limit: int | None = None,
               every: int = 1,
               reverse_cts: bool | Iterable[str] = False) -> dict:
    files = find_session_files(session_dir)
    config = {}
    if files["config_json"].exists():
        config = json.loads(files["config_json"].read_text())

    headers = ["record_index", "timestamp_utc", "window_end_utc"]
    headers += [f.name for f in FIELDS]
    # reverse_cts may be True (all phases), False (none), or an iterable of
    # phase letters; reverse_cts_indices() handles all of those.
    flip = reverse_cts_indices(reverse_cts if reverse_cts else False)

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


def estimate_record_count(trend_path: Path) -> int:
    """Cheap upper-bound record count from file size (no parse)."""
    try:
        return trend_path.stat().st_size // RECORD_SIZE
    except OSError:
        return 0


def export_csv_multi(
    session_dir: Path,
    full_path: Path,
    min_path: Path,
    every: int = 1,
    reverse_cts: bool | Iterable[str] = False,
    max_full_rows: int | None = None,
    time_shift: "dt.timedelta | None" = None,
    build_store: bool = True,
    log=None,
    progress_every: int = 0,
):
    """Single streaming pass: write full + 1-min CSVs and (optionally) a store.

    The 438 MB binary used to be parsed twice (once per ``export_csv`` call).
    This walks ``trend.bin`` exactly once and fans the decoded records out to:

    - ``full_path``: every ``effective_every``-th record;
    - ``min_path``: one row per minute (every 60 source records), always full
      coverage regardless of the row guard;
    - an in-memory :class:`~fluke_3540.store.ColumnStore` (when ``build_store``),
      built from EVERY record so analysis is never downsampled.

    Large-session guard: if the projected full-resolution row count
    (``record_count / every``) exceeds ``max_full_rows``, the full CSV's stride
    is raised so it lands at/under the cap, and exactly what was downsampled is
    logged. The 1-min CSV and the store are unaffected.

    ``time_shift`` (e.g. from an ``--anchor-*`` flag) is applied to the CSV
    timestamps and stamped into the store so the on-disk and in-memory views
    agree on wall-clock.

    Returns a dict with ``config``, ``rows_written_full``, ``rows_written_min``,
    ``store`` (or None), ``parse_stats``, ``effective_every``, ``downsampled``.
    """
    # Local import avoids a parser->store->parser cycle at module load.
    from .store import STORE_COLUMNS, ColumnStore, _resolve_indices

    files = find_session_files(session_dir)
    config = {}
    if files["config_json"].exists():
        config = json.loads(files["config_json"].read_text())

    headers = ["record_index", "timestamp_utc", "window_end_utc"]
    headers += [f.name for f in FIELDS]
    flip = reverse_cts_indices(reverse_cts if reverse_cts else False)
    shift = time_shift or dt.timedelta(0)

    # Row-cap guard: raise the full-CSV stride if projected rows exceed the cap.
    est = estimate_record_count(files["trend"])
    effective_every = max(1, every)
    downsampled = False
    if max_full_rows is not None and max_full_rows > 0 and est > 0:
        projected = est // effective_every
        if projected > max_full_rows:
            import math as _math
            needed = _math.ceil(est / max_full_rows)
            effective_every = max(effective_every, needed)
            downsampled = True
            if log:
                log(f"[parse] full CSV would be ~{projected:,} rows "
                    f"(> --max-csv-rows {max_full_rows:,}); raising stride "
                    f"from every={every} to every={effective_every} "
                    f"(~{est // effective_every:,} rows). 1-min CSV and "
                    f"analysis store keep full resolution.")

    store = ColumnStore(time_shift=shift) if build_store else None
    col_idx = _resolve_indices(STORE_COLUMNS) if build_store else []

    stats = ParseStats()
    rows_full = 0
    rows_min = 0
    first_ts = None
    last_ts = None

    full_path.parent.mkdir(parents=True, exist_ok=True)
    min_path.parent.mkdir(parents=True, exist_ok=True)

    with full_path.open("w", newline="") as ff, \
         min_path.open("w", newline="") as fm:
        wf = csv.writer(ff)
        wm = csv.writer(fm)
        wf.writerow(headers)
        wm.writerow(headers)
        for rec in iter_records_safe(files["trend"], stats=stats, log=log):
            s_shift = rec.start + shift
            e_shift = rec.end + shift
            if first_ts is None:
                first_ts = s_shift
            last_ts = e_shift

            # Build a flipped row lazily only when we actually write it.
            def make_row():
                row = [rec.index, s_shift.isoformat(), e_shift.isoformat()]
                for f in FIELDS:
                    v = rec.floats[f.index]
                    if f.index in flip:
                        v = -v
                    row.append(v)
                return row

            if rec.index % effective_every == 0:
                wf.writerow(make_row())
                rows_full += 1
            if rec.index % 60 == 0:
                wm.writerow(make_row())
                rows_min += 1

            if store is not None:
                cols = store._cols
                floats = rec.floats
                for name, idx in zip(STORE_COLUMNS, col_idx):
                    # Flip store columns too so analysis sees the same sign as
                    # the CSV (P/Q/PF totals depend on CT orientation).
                    v = floats[idx]
                    if idx in flip:
                        v = -v
                    cols[name].append(v)
                store._start_ticks.append(_filetime_ticks(rec.start))
                store._end_ticks.append(_filetime_ticks(rec.end))
                store._n += 1

            if progress_every and log and stats.good % progress_every == 0:
                pct = (stats.good / est * 100) if est else 0.0
                log(f"[parse]   {stats.good:,}/{est:,} records ({pct:.0f}%)")

    return {
        "config": config,
        "rows_written_full": rows_full,
        "rows_written_min": rows_min,
        "store": store,
        "parse_stats": stats,
        "effective_every": effective_every,
        "downsampled": downsampled,
        "first_ts": first_ts.isoformat() if first_ts else None,
        "last_ts": last_ts.isoformat() if last_ts else None,
        "reversed_columns": sorted(f.name for f in FIELDS if f.index in flip),
    }


def _filetime_ticks(value: dt.datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return int(round((value - FILETIME_EPOCH).total_seconds() * 10_000_000))


def _parse_reverse_cts_arg(raw: str | None) -> bool | list[str]:
    """Convert the CLI's --reverse-cts argument value into the API form.

    None         -> False (no flip)
    "all"        -> True  (all phases, returned by ``nargs='?' const='all'``)
    "a" / "a,c"  -> ["a"] / ["a", "c"]
    """
    if raw is None:
        return False
    if raw == "all":
        return True
    phases = [p.strip().lower() for p in raw.split(",") if p.strip()]
    bad = [p for p in phases if p not in _VALID_PHASES]
    if bad:
        raise SystemExit(
            f"--reverse-cts: bad phase(s) {bad}. Use any subset of a, b, c."
        )
    return phases


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
    ap.add_argument("--reverse-cts", nargs="?", const="all", default=None,
                    metavar="PHASES",
                    help="Negate P/P1/Q/Q1/PF/DPF/Wh/VARh to correct for physically "
                         "reversed iFlex CT probes. Bare flag = all phases; pass a "
                         "comma list like 'a,c' to only flip those phases (plus totals).")
    args = ap.parse_args(argv)

    if not args.session_dir.is_dir():
        print(f"ERROR: {args.session_dir} is not a directory", file=sys.stderr)
        return 1
    output = args.output or args.session_dir.with_suffix(".csv")
    reverse_cts_arg = _parse_reverse_cts_arg(args.reverse_cts)
    result = export_csv(
        args.session_dir, output,
        limit=args.limit, every=args.every, reverse_cts=reverse_cts_arg,
    )
    print(f"Wrote {output}")
    print(f"  rows: {result['rows_written']}")
    print(f"  columns: {result['columns']}")
    print(f"  time range (UTC): {result['first_ts']} .. {result['last_ts']}")
    if result["reverse_cts"]:
        flipped_phases = (
            "all phases" if reverse_cts_arg is True
            else "phase(s) " + ",".join(sorted(reverse_cts_arg))
        )
        print(f"  --reverse-cts ({flipped_phases}): negated "
              f"{len(result['reversed_columns'])} columns "
              "(P*/Q*/PF*/DPF*/Wh*/VARh*)")
    cfg = result["config"]
    if cfg:
        print(f"  asset: {cfg.get('asset_name')}  team: {cfg.get('team_name')}")
        print(f"  instrument: {cfg.get('type')}  fw={cfg.get('firmware_version')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
