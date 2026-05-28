"""``fluke-analyze compare`` subcommand — overlay charts across N sessions."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .parser import _parse_reverse_cts_arg, export_csv, open_session
from .plots import GnuplotNotFound
from .plots.compare import COMPARE_QUANTITIES, render_compare


def build_compare_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="fluke-analyze compare",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("sessions", nargs="+", type=Path,
                    help="Two or more session inputs (ES.NNN/ dirs or .fel files)")
    ap.add_argument("-o", "--output", required=True, type=Path,
                    help="Output directory (will be created)")
    ap.add_argument("--labels", type=str, default=None,
                    help="Comma-separated display labels per session "
                         "(default: derived from each input's name)")
    ap.add_argument("--plot", type=str, default=None,
                    help=f"Quantities to compare (default all). "
                         f"Valid: {','.join(sorted(COMPARE_QUANTITIES))}")
    ap.add_argument("--reverse-cts", nargs="?", const="all", default=None,
                    metavar="PHASES",
                    help="Apply same reverse-CTS phases to every session")
    ap.add_argument("--format", choices=("png", "svg"), default="png",
                    help="Image format (default png)")
    return ap


def _resolve_labels(arg: str | None, sessions: Sequence[Path]) -> list[str]:
    if arg:
        labels = [s.strip() for s in arg.split(",")]
        if len(labels) != len(sessions):
            raise SystemExit(
                f"--labels count ({len(labels)}) does not match sessions count "
                f"({len(sessions)})"
            )
        return labels
    out: list[str] = []
    seen: dict[str, int] = {}
    for s in sessions:
        base = s.name
        if base.lower().endswith(".fel"):
            base = base[:-4]
        if base in seen:
            seen[base] += 1
            base = f"{base}-{seen[base]}"
        else:
            seen[base] = 1
        out.append(base)
    return out


def compare_main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    args = build_compare_argparser().parse_args(argv)
    if len(args.sessions) < 2:
        print("ERROR: compare requires at least 2 sessions", file=sys.stderr)
        return 1
    for s in args.sessions:
        if not s.exists():
            print(f"ERROR: session not found: {s}", file=sys.stderr)
            return 1

    labels = _resolve_labels(args.labels, args.sessions)
    outdir: Path = args.output
    outdir.mkdir(parents=True, exist_ok=True)
    reverse_cts = _parse_reverse_cts_arg(args.reverse_cts)
    quantities = (
        [q.strip() for q in args.plot.split(",") if q.strip()]
        if args.plot else list(COMPARE_QUANTITIES.keys())
    )
    bad = [q for q in quantities if q not in COMPARE_QUANTITIES]
    if bad:
        print(f"ERROR: unknown quantities {bad}. "
              f"Valid: {sorted(COMPARE_QUANTITIES)}", file=sys.stderr)
        return 1

    # Phase 1: parse each session into outdir/session_N.csv
    session_csvs: list[Path] = []
    for i, session_input in enumerate(args.sessions):
        csv_out = outdir / f"session_{i}.csv"
        print(f"[parse] {session_input}  →  {csv_out}  (label: {labels[i]})")
        with open_session(session_input) as session_dir:
            result = export_csv(
                session_dir, csv_out, reverse_cts=reverse_cts,
            )
        print(f"        {result['rows_written']:,} rows")
        session_csvs.append(csv_out)

    try:
        # Phase 2: overlay charts + summary CSV
        print(f"[render] overlay charts → {outdir}/  ({', '.join(quantities)})")
        cmp = render_compare(
            session_csvs, labels, outdir,
            quantities=quantities, image_format=args.format,
        )
        print(f"         {len(cmp.chart_paths)} chart(s) written")
        print(f"         summary → {cmp.summary_csv}")
    except GnuplotNotFound as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"[done] {outdir}")
    return 0
