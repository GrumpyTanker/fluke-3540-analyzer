"""``fluke-analyze stitch`` subcommand — concatenate consecutive sessions.

Stitches two or more sessions (same asset, consecutive captures) into one
continuous timeline that beats the meter's 7-day cap, then runs the normal
analysis over the stitched series: events.json, snapshots.json, insights.json,
stats.json, a stitched session CSV, and a stitch.json provenance file.
"""
from __future__ import annotations

import argparse
import csv as _csv
import json
import sys
from pathlib import Path
from typing import Sequence

from .analysis import whole_session_stats
from .events import detect_events
from .insights import analyze as analyze_insights, to_jsonable as finding_to_jsonable
from .parser import (
    _parse_reverse_cts_arg, from_csv, iter_records, open_session,
    reverse_cts_indices,
)
from .snapshots import pick_snapshots
from .stitch import stitch_stores
from .store import STORE_COLUMNS, ColumnStore, _resolve_indices


def build_stitch_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="fluke-analyze stitch",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("sessions", nargs="+", type=Path,
                    help="Two or more session inputs (ES.NNN/ dirs, .fel, or .csv)")
    ap.add_argument("-o", "--output", required=True, type=Path,
                    help="Output directory (will be created)")
    ap.add_argument("--labels", type=str, default=None,
                    help="Comma-separated labels per session (default: input names)")
    ap.add_argument("--reverse-cts", nargs="?", const="all", default=None,
                    metavar="PHASES",
                    help="Apply the same reverse-CTS phases to every session")
    ap.add_argument("--gap-tolerance", type=float, default=2.0, metavar="SECS",
                    help="Boundary gaps larger than this are recorded (default 2 s)")
    ap.add_argument("--nominal-ln-v", type=float, default=None, metavar="V",
                    help="Nominal L-N voltage (auto-inferred if omitted)")
    ap.add_argument("--no-stats", action="store_true",
                    help="Skip whole-session statistics over the stitched series")
    ap.add_argument("--no-csv", action="store_true",
                    help="Skip writing the (large) stitched session.csv")
    return ap


def _labels(arg: str | None, sessions: Sequence[Path]) -> list[str]:
    if arg:
        labels = [s.strip() for s in arg.split(",")]
        if len(labels) != len(sessions):
            raise SystemExit(
                f"--labels count ({len(labels)}) != sessions count ({len(sessions)})")
        return labels
    out: list[str] = []
    seen: dict[str, int] = {}
    for s in sessions:
        base = s.name
        for suf in (".fel", ".csv"):
            if base.lower().endswith(suf):
                base = base[:-len(suf)]
                break
        if base in seen:
            seen[base] += 1
            base = f"{base}-{seen[base]}"
        else:
            seen[base] = 1
        out.append(base)
    return out


def _store_for(session_input: Path, reverse_cts) -> ColumnStore:
    """Build a ColumnStore for one session input (dir/.fel/.csv)."""
    if session_input.is_file() and session_input.suffix.lower() == ".csv":
        return ColumnStore.from_records(from_csv(session_input))
    flip = reverse_cts_indices(reverse_cts if reverse_cts else False)
    col_idx = _resolve_indices(STORE_COLUMNS)
    with open_session(session_input) as session_dir:
        from .parser import find_session_files
        trend = find_session_files(session_dir)["trend"]
        store = ColumnStore()
        for rec in iter_records(trend):
            floats = rec.floats
            for name, idx in zip(STORE_COLUMNS, col_idx):
                v = floats[idx]
                if idx in flip:
                    v = -v
                store._cols[name].append(v)
            from .parser import _filetime_ticks
            store._start_ticks.append(_filetime_ticks(rec.start))
            store._end_ticks.append(_filetime_ticks(rec.end))
            store._n += 1
    return store


def _write_stitched_csv(path: Path, store: ColumnStore,
                        sources, gaps) -> None:
    """Write the stitched series CSV with a `source` provenance column."""
    # Map record index -> source label.
    label_of = [""] * store.n
    for s in sources:
        for i in range(s.lo, s.hi):
            label_of[i] = s.label
    cols = list(store.columns)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(["timestamp_utc", "window_end_utc", "source", *cols])
        col_arrays = [store.col(c) for c in cols]
        for i in range(store.n):
            w.writerow([store.start(i).isoformat(), store.end(i).isoformat(),
                        label_of[i], *[ca[i] for ca in col_arrays]])


def stitch_main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    args = build_stitch_argparser().parse_args(argv)
    if len(args.sessions) < 2:
        print("ERROR: stitch requires at least 2 sessions", file=sys.stderr)
        return 1
    for s in args.sessions:
        if not s.exists():
            print(f"ERROR: session not found: {s}", file=sys.stderr)
            return 1

    labels = _labels(args.labels, args.sessions)
    reverse_cts = _parse_reverse_cts_arg(args.reverse_cts)
    outdir: Path = args.output
    outdir.mkdir(parents=True, exist_ok=True)

    labelled: list[tuple[str, ColumnStore]] = []
    for lbl, session_input in zip(labels, args.sessions):
        print(f"[stitch] parsing {session_input}  (label: {lbl})")
        st = _store_for(session_input, reverse_cts)
        print(f"         {st.n:,} records")
        labelled.append((lbl, st))

    result = stitch_stores(labelled, gap_tolerance_secs=args.gap_tolerance)
    store = result.store
    print(f"[stitch] stitched {len(result.sources)} sessions → {store.n:,} records, "
          f"{len(result.gaps)} gap(s)")
    for g in result.gaps:
        print(f"         GAP between {g.after_label} and {g.before_label}: "
              f"{g.seconds:.0f}s ({g.t_gap_start} .. {g.t_gap_end})")

    (outdir / "stitch.json").write_text(
        json.dumps(result.to_jsonable(), indent=2), encoding="utf-8")

    # Analysis over the stitched timeline.
    events = detect_events(store, nominal_ln_v=args.nominal_ln_v)
    snaps = pick_snapshots(store, events, n=3)
    findings = analyze_insights(store, events, snaps, {})
    print(f"[detect] {len(events)} events, {len(snaps)} snapshots, "
          f"{len(findings)} insight(s) over the stitched series")

    def _event_json(ev):
        return {
            "id": ev.id, "kind": ev.kind,
            "t_start": ev.t_start.isoformat(), "t_end": ev.t_end.isoformat(),
            "severity": ev.severity, "affected_phases": list(ev.affected_phases),
        }
    (outdir / "events.json").write_text(
        json.dumps([_event_json(e) for e in events], indent=2), encoding="utf-8")
    (outdir / "insights.json").write_text(
        json.dumps([finding_to_jsonable(f) for f in findings], indent=2),
        encoding="utf-8")

    if not args.no_stats:
        stats = whole_session_stats(store)
        (outdir / "stats.json").write_text(
            json.dumps(stats, indent=2), encoding="utf-8")
        print("[stats] wrote stats.json over the stitched series")

    if not args.no_csv:
        csv_path = outdir / "session.csv"
        _write_stitched_csv(csv_path, store, result.sources, result.gaps)
        print(f"[stitch] wrote stitched {csv_path.name} "
              f"({store.n:,} rows, with source provenance column)")

    print(f"[done] {outdir}")
    return 0
