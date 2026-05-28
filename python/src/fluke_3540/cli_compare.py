"""``fluke-analyze compare`` subcommand — overlay charts across N sessions."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .events import detect_events
from .insights import analyze as analyze_insights
from .insights_compare import analyze_compare, to_jsonable as compare_finding_to_jsonable
from .parser import _parse_reverse_cts_arg, export_csv, from_csv, iter_records, open_session
from .plots import (
    GnuplotNotFound, WeasyPrintNotInstalled,
    write_compare_html_report, write_pdf_report,
)
from .plots.compare import COMPARE_QUANTITIES, render_compare
from .snapshots import pick_snapshots


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
    ap.add_argument("--no-html", action="store_true",
                    help="Skip the self-contained compare HTML report")
    ap.add_argument("--pdf", action="store_true",
                    help="Also render compare.pdf via weasyprint")
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
    session_records: list[list] = []
    for i, session_input in enumerate(args.sessions):
        csv_out = outdir / f"session_{i}.csv"
        print(f"[parse] {session_input}  →  {csv_out}  (label: {labels[i]})")
        if session_input.is_file() and session_input.suffix.lower() == ".csv":
            if session_input.resolve() != csv_out.resolve():
                csv_out.write_bytes(session_input.read_bytes())
            session_records.append(list(from_csv(csv_out)))
        else:
            with open_session(session_input) as session_dir:
                result = export_csv(
                    session_dir, csv_out, reverse_cts=reverse_cts,
                )
                from .parser import find_session_files
                trend = find_session_files(session_dir)["trend"]
                session_records.append(list(iter_records(trend)))
            print(f"        {result['rows_written']:,} rows")
        session_csvs.append(csv_out)

    # Phase 1b: run single-session detection + cross-session insights
    print("[detect] computing per-session events + cross-session insights…")
    session_packs = []
    for label, recs in zip(labels, session_records):
        events = detect_events(recs)
        findings = analyze_insights(recs, events)
        session_packs.append({
            "label": label, "records": recs,
            "events": events, "findings": findings,
        })
    cross_findings = analyze_compare(session_packs)
    (outdir / "compare_insights.json").write_text(
        json.dumps([compare_finding_to_jsonable(f) for f in cross_findings], indent=2),
        encoding="utf-8",
    )
    print(f"         {len(cross_findings)} cross-session finding(s) "
          f"→ compare_insights.json")

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

    # Phase 3: HTML / PDF reports
    html_path = outdir / "compare_report.html"
    if not args.no_html:
        print(f"[render] compare HTML → {html_path}")
        write_compare_html_report(
            html_path, output_dir=outdir,
            session_stats=[{"label": lbl, **s}
                           for lbl, s in zip(labels, cmp.per_session_stats)],
            findings=cross_findings,
        )

    if args.pdf:
        pdf_path = outdir / "compare_report.pdf"
        if not html_path.is_file():
            print("ERROR: --pdf requires the compare HTML report (don't combine with --no-html)",
                  file=sys.stderr)
            return 2
        print(f"[render] compare PDF → {pdf_path}")
        try:
            write_pdf_report(html_path, pdf_path)
        except WeasyPrintNotInstalled as e:
            print(f"ERROR: {e}", file=sys.stderr)

    print(f"[done] {outdir}")
    return 0
