"""fluke-analyze — orchestrate parse → detect → render for a Fluke session.

Modes:
    --auto         (default) parse, detect, render everything
    --interactive  prompt-driven pickers
    --parse-only   produce CSV + events.json, no plots
    --plot-only    reuse existing CSV + events.json, just render
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Sequence

import csv as _csv

from .events import Event, detect_events
from .insights import Finding, analyze as analyze_insights, to_jsonable as finding_to_jsonable
from .parser import (
    _parse_reverse_cts_arg, compute_time_shift, export_csv, export_csv_multi,
    find_session_files, from_csv, iter_records, open_session,
)
from .store import ColumnStore
from .plots import (
    GnuplotNotFound, WeasyPrintNotInstalled,
    render_event_zoom, render_full_session, render_snapshot_zoom,
    write_html_report, write_pdf_report, write_xlsx,
)
from .plots.full_session import QUANTITY_SPECS as FULL_QUANTITIES
from .snapshots import Snapshot, pick_snapshots


DEFAULT_PLOTS = ("voltage", "current", "power", "thd", "pf", "frequency")
DEFAULT_ZOOM_PLOTS = ("voltage", "current", "power")


def _isoformat(d: dt.datetime) -> str:
    return d.isoformat()


def _parse_time(text: str) -> dt.datetime:
    """Accept ISO-8601 with or without timezone; treat naive as UTC."""
    t = dt.datetime.fromisoformat(text)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t


def _event_to_json(ev: Event) -> dict:
    d = asdict(ev)
    d["t_start"] = _isoformat(ev.t_start)
    d["t_end"] = _isoformat(ev.t_end)
    d["affected_phases"] = list(ev.affected_phases)
    return d


def _event_from_json(d: dict) -> Event:
    return Event(
        id=d["id"], kind=d["kind"],
        t_start=_parse_time(d["t_start"]),
        t_end=_parse_time(d["t_end"]),
        severity=d["severity"],
        affected_phases=tuple(d["affected_phases"]),
    )


def _snapshot_to_json(s: Snapshot) -> dict:
    return {
        "id": s.id,
        "t_start": _isoformat(s.t_start),
        "t_end": _isoformat(s.t_end),
        "t_center": _isoformat(s.t_center),
        "p_total_mean_w": s.p_total_mean_w,
        "p_total_stdev_w": s.p_total_stdev_w,
    }


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="fluke-analyze", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("session_dir", type=Path,
                    help="Path to an ES.NNN session directory, a .fel zip-bundle, "
                         "or a pre-parsed session.csv")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="Output directory (default: <session>_out next to the session)")

    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--auto", action="store_true",
                      help="Default — parse, detect, render everything")
    mode.add_argument("--interactive", action="store_true",
                      help="Prompt for event/quantity/phase picks")
    mode.add_argument("--parse-only", action="store_true",
                      help="Write CSV + events.json + summary; no charts")
    mode.add_argument("--plot-only", action="store_true",
                      help="Reuse existing CSV + events.json; render charts only")
    mode.add_argument("--json", action="store_true", dest="json_mode",
                      help="Parse + detect, emit a single JSON blob to stdout, "
                           "no charts, no logs. Useful for piping into jq.")

    # Filters
    ap.add_argument("--from", dest="from_time", type=_parse_time, default=None,
                    metavar="ISO_TIME", help="Restrict event scan & zooms to ≥ this time")
    ap.add_argument("--to", dest="to_time", type=_parse_time, default=None,
                    metavar="ISO_TIME", help="Restrict event scan & zooms to ≤ this time")
    ap.add_argument("--pre", type=int, default=30, metavar="SECS",
                    help="Pre-event zoom padding in seconds (default 30)")
    ap.add_argument("--post", type=int, default=60, metavar="SECS",
                    help="Post-event zoom padding in seconds (default 60)")
    ap.add_argument("--events", type=str, default=None, metavar="IDS",
                    help="Comma-separated event IDs to render (default: all)")
    ap.add_argument("--plot", type=str, default=None, metavar="QTYS",
                    help=f"Comma-separated quantities to chart "
                         f"(default: {','.join(DEFAULT_PLOTS)}). "
                         f"Valid: {','.join(sorted(FULL_QUANTITIES))}")
    ap.add_argument("--phase", type=str, default=None, metavar="PHASES",
                    help="Comma-separated phases (a,b,c,total). Reserved for future "
                         "per-phase chart filtering; currently informational.")
    ap.add_argument("--snapshots", type=int, default=3, metavar="N",
                    help="Number of normal-operation snapshots to pick (default 3)")

    # Parse options
    ap.add_argument("--reverse-cts", nargs="?", const="all", default=None,
                    metavar="PHASES",
                    help="Negate P/Q/PF/DPF/Wh/VARh for backwards iFlex CTs. "
                         "Bare flag = all phases; pass a comma list like 'a,c' to "
                         "only flip those phases (plus totals).")
    ap.add_argument("--every", type=int, default=1, metavar="K",
                    help="Emit every K-th record into the CSV (default 1, all)")
    ap.add_argument("--max-csv-rows", type=int, default=None, metavar="N",
                    help="Cap the full-resolution CSV at ~N rows. If the session "
                         "would exceed it, the stride is raised and the downsampling "
                         "is logged. The 1-min CSV and analysis keep full resolution.")

    # Clock-correction anchors (mutually exclusive)
    anchor = ap.add_mutually_exclusive_group()
    anchor.add_argument("--anchor-start", dest="anchor_start", type=_parse_time,
                        default=None, metavar="ISO_TIME",
                        help="Pin the real wall-clock START to this time, correcting "
                             "a wrong meter RTC. Shifts every timestamp by the delta.")
    anchor.add_argument("--anchor-end", dest="anchor_end", type=_parse_time,
                        default=None, metavar="ISO_TIME",
                        help="Pin the real wall-clock END to this time (mutually "
                             "exclusive with --anchor-start).")

    # Time-bucket splitting
    ap.add_argument("--split-by", dest="split_by", type=str, default=None,
                    metavar="PERIOD",
                    help="Partition the session into time buckets, emitting a full "
                         "per-bucket report plus a roll-up. PERIOD: hour|day|week or "
                         "a duration like 30m, 6h, 2d.")

    # Event markers / correlation
    ap.add_argument("--mark", action="append", default=None, metavar="ISO=LABEL",
                    help="Add an event marker 'ISO_TIME=label' (repeatable). Anchored "
                         "timeline applies. Cross-referenced against detected events.")
    ap.add_argument("--marks", type=Path, default=None, metavar="FILE.csv",
                    help="CSV of markers with columns time,label.")

    # Whole-session statistics + diurnal profile
    ap.add_argument("--no-stats", action="store_true",
                    help="Skip whole-session statistics (stats.json/csv).")
    ap.add_argument("--tod-profile", dest="tod_profile", nargs="?",
                    const="00:00-24:00", default=None, metavar="HH:MM-HH:MM",
                    help="Build a time-of-day (diurnal) profile. Bare flag = full "
                         "24 h; pass a window like 08:00-17:00.")
    ap.add_argument("--tod-bin", dest="tod_bin", type=int, default=1, metavar="MINS",
                    help="Time-of-day bin width in minutes (default 1).")

    # Output knobs
    ap.add_argument("--no-xlsx", action="store_true", help="Skip XLSX report")
    ap.add_argument("--no-html", action="store_true",
                    help="Skip the self-contained HTML report (default writes report.html)")
    ap.add_argument("--pdf", action="store_true",
                    help="Also render report.pdf via weasyprint (install with "
                         "`pip install fluke-3540-analyzer[pdf]`)")
    ap.add_argument("--no-overview", action="store_true",
                    help="Skip the overview multiplot")
    ap.add_argument("--format", choices=("png", "svg"), default="png",
                    help="Chart image format (default png)")
    ap.add_argument("--nominal-ln-v", type=float, default=None, metavar="V",
                    help="Nominal L-N voltage (auto-inferred if omitted)")

    return ap


def _is_csv_input(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".csv"


def _resolve_outdir(args: argparse.Namespace) -> Path:
    if args.output:
        return args.output
    name = args.session_dir.name
    for suf in (".fel", ".csv"):
        if name.lower().endswith(suf):
            name = name[:-len(suf)]
            break
    return args.session_dir.parent / (name + "_out")


def _downsample_csv(src: Path, dst: Path, every: int) -> int:
    """Write a CSV containing only every Nth data row from src; header preserved."""
    if every <= 1:
        dst.write_bytes(src.read_bytes())
        return -1
    rows = 0
    with src.open("r", newline="", encoding="utf-8") as inp, \
         dst.open("w", newline="", encoding="utf-8") as out:
        reader = _csv.reader(inp)
        writer = _csv.writer(out)
        header = next(reader, None)
        if header is not None:
            writer.writerow(header)
        for i, row in enumerate(reader):
            if i % every == 0:
                writer.writerow(row)
                rows += 1
    return rows


def _parse_csv_session(args: argparse.Namespace, outdir: Path,
                      ) -> tuple[Path, Path, dict]:
    """CSV-input replacement for _parse_session: copies + downsamples."""
    full_csv = outdir / "session.csv"
    min_csv = outdir / "session_1min.csv"
    if args.session_dir.resolve() != full_csv.resolve():
        full_csv.write_bytes(args.session_dir.read_bytes())
    print(f"[csv-in] {args.session_dir}  →  {full_csv}")
    _downsample_csv(full_csv, min_csv, every=max(60, args.every))
    return full_csv, min_csv, {}  # config unavailable for CSV input


def _parse_session(args: argparse.Namespace, outdir: Path,
                   session_dir: Path,
                   ) -> tuple[Path, Path, dict, ColumnStore]:
    """Phase 1A: single-pass parse trend.bin → CSVs + ColumnStore.

    Walks the binary exactly once (was twice), building the full CSV, the 1-min
    CSV, and the in-memory analysis store together. Applies the --anchor-* clock
    correction and the --max-csv-rows guard.
    """
    full_csv = outdir / "session.csv"
    min_csv = outdir / "session_1min.csv"
    reverse_cts = _parse_reverse_cts_arg(args.reverse_cts)
    trend = find_session_files(session_dir)["trend"]

    time_shift = compute_time_shift(
        trend,
        anchor_start=getattr(args, "anchor_start", None),
        anchor_end=getattr(args, "anchor_end", None),
    )
    if time_shift:
        print(f"[parse] clock anchor: shifting timestamps by {time_shift}")

    print(f"[parse] {args.session_dir}  →  {full_csv} (+ 1-min, single pass)")
    res = export_csv_multi(
        session_dir, full_csv, min_csv,
        every=args.every, reverse_cts=reverse_cts,
        max_full_rows=getattr(args, "max_csv_rows", None),
        time_shift=time_shift, build_store=True,
        log=print, progress_every=100_000,
    )
    st = res["parse_stats"]
    print(f"        {res['rows_written_full']:,} full rows, "
          f"{res['rows_written_min']:,} 1-min rows, store n={res['store'].n:,}")
    if st.bad_magic or st.truncated or st.nonfinite:
        print(f"        robustness: {st.bad_magic} bad-magic, "
              f"{st.truncated} truncated, {st.nonfinite} non-finite (skipped/flagged)")
    return full_csv, min_csv, res["config"], res["store"]


def _store_from_csv(csv_path: Path) -> ColumnStore:
    """Build a ColumnStore from a previously-exported session CSV (streaming)."""
    return ColumnStore.from_records(from_csv(csv_path))


def _window_filter_store(store: ColumnStore, from_time, to_time) -> ColumnStore:
    """Return a store restricted to records within [from_time, to_time].

    Operates on the already-shifted timeline (store.start/end apply time_shift).
    Cheap: copies only the retained columns, not raw Records.
    """
    if not from_time and not to_time:
        return store
    keep = ColumnStore(time_shift=store.time_shift)
    cols = store._cols
    keep_cols = keep._cols
    for i in range(store.n):
        s = store.start(i)
        e = store.end(i)
        if from_time and s < from_time:
            continue
        if to_time and e > to_time:
            continue
        for name in store.columns:
            keep_cols[name].append(cols[name][i])
        keep._start_ticks.append(store._start_ticks[i])
        keep._end_ticks.append(store._end_ticks[i])
        keep._n += 1
    return keep


def _detect_and_save(args: argparse.Namespace, outdir: Path,
                     trend_path: Path | None,
                     config: dict | None = None,
                     *,
                     csv_path: Path | None = None,
                     store: ColumnStore | None = None,
                     ) -> tuple[list[Event], list[Snapshot], list[Finding], ColumnStore]:
    """Phase 1B: run event + snapshot + insights detection, write JSON.

    Pass a prebuilt store (binary input), or csv_path (CSV input). Returns the
    (possibly window-filtered) store alongside the detections.
    """
    print("[detect] running event scan…")
    if store is None:
        if csv_path is not None:
            store = _store_from_csv(csv_path)
        else:
            store = ColumnStore.from_records(iter_records(trend_path))
    if args.from_time or args.to_time:
        before = store.n
        store = _window_filter_store(store, args.from_time, args.to_time)
        print(f"         window filter: {before:,} → {store.n:,} records")
    events = detect_events(store, nominal_ln_v=args.nominal_ln_v)
    snaps = pick_snapshots(store, events, n=args.snapshots)
    findings = analyze_insights(store, events, snaps, config or {})
    print(f"         {len(events)} events, {len(snaps)} snapshots, "
          f"{len(findings)} insight(s)")

    events_path = outdir / "events.json"
    snaps_path = outdir / "snapshots.json"
    insights_path = outdir / "insights.json"
    events_path.write_text(json.dumps(
        [_event_to_json(e) for e in events], indent=2,
    ), encoding="utf-8")
    snaps_path.write_text(json.dumps(
        [_snapshot_to_json(s) for s in snaps], indent=2,
    ), encoding="utf-8")
    insights_path.write_text(json.dumps(
        [finding_to_jsonable(f) for f in findings], indent=2,
    ), encoding="utf-8")
    print(f"         wrote {events_path.name}, {snaps_path.name}, {insights_path.name}")
    return events, snaps, findings, store


def _run_extra_analyses(args: argparse.Namespace, outdir: Path,
                        store: ColumnStore,
                        events: Sequence[Event],
                        findings: Sequence[Finding],
                        config: dict,
                        full_csv: Path, min_csv: Path) -> None:
    """Run the round-2 analysis features that depend on the in-memory store.

    Each feature is opt-in via its flag; this is the single hook the CLI calls
    after detection. Features are filled in incrementally (markers, per-bucket
    summary, stats, time-of-day, split-by). Kept side-effect-light so it is a
    no-op when no relevant flags are set.
    """
    return None


def _write_summary_txt(outdir: Path, events: Sequence[Event],
                       snaps: Sequence[Snapshot],
                       findings: Sequence[Finding],
                       config: dict) -> None:
    lines: list[str] = ["Fluke 3540 FC Session Summary", "=" * 32, ""]
    if config:
        if config.get("asset_name"):
            lines.append(f"Asset:       {config['asset_name']}")
        if config.get("team_name"):
            lines.append(f"Team:        {config['team_name']}")
        if config.get("type"):
            lines.append(f"Instrument:  {config['type']}  fw={config.get('firmware_version', '?')}")
        lines.append("")
    if findings:
        lines.append("Insights")
        lines.append("-" * 8)
        for f in findings:
            lines.append(f"  [{f.severity:5s}] {f.kind:25s}  {f.headline}")
        lines.append("")
    lines.append(f"Events detected: {len(events)}")
    for ev in events:
        phases = "/".join(ev.affected_phases) or "—"
        lines.append(f"  #{ev.id:>3}  {ev.kind:18s}  {ev.t_start.isoformat()}  "
                     f"phases={phases}  severity={ev.severity:.3f}")
    lines.append("")
    lines.append(f"Quiet snapshots: {len(snaps)}")
    for s in snaps:
        lines.append(f"  #{s.id:>3}  {s.t_start.isoformat()}  "
                     f"mean P={s.p_total_mean_w / 1000:+.2f} kW  σ={s.p_total_stdev_w:.1f} W")
    (outdir / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _filter_events(events: Sequence[Event], ids_arg: str | None) -> list[Event]:
    if not ids_arg:
        return list(events)
    wanted = {int(x.strip()) for x in ids_arg.split(",") if x.strip()}
    return [e for e in events if e.id in wanted]


def _parse_quantities(arg: str | None, default: Sequence[str],
                      valid: Iterable[str]) -> list[str]:
    if not arg:
        return list(default)
    picked = [q.strip() for q in arg.split(",") if q.strip()]
    bad = [q for q in picked if q not in valid]
    if bad:
        raise SystemExit(
            f"Unknown chart quantities: {bad}. Valid: {sorted(valid)}"
        )
    return picked


def _render_phase(args: argparse.Namespace, outdir: Path, full_csv: Path,
                  min_csv: Path, events: Sequence[Event],
                  snaps: Sequence[Snapshot], config: dict) -> None:
    full_qtys = _parse_quantities(args.plot, DEFAULT_PLOTS, FULL_QUANTITIES.keys())
    # Subset of zoom quantities that overlap with the user's --plot selection.
    zoom_qtys = [q for q in DEFAULT_ZOOM_PLOTS if q in full_qtys] or DEFAULT_ZOOM_PLOTS
    selected_events = _filter_events(events, args.events)

    charts_dir = outdir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    print(f"[render] full-session charts → {charts_dir}/  ({', '.join(full_qtys)})")
    fs_result = render_full_session(
        full_csv, charts_dir,
        quantities=full_qtys,
        include_overview=not args.no_overview,
        image_format=args.format,
        overview_title=(
            f"Fluke 3540 FC — {config.get('asset_name', 'Session')} Overview"
        ),
    )
    print(f"         {len(fs_result.chart_paths)} chart(s) written")

    if selected_events:
        print(f"[render] event zooms ({len(selected_events)} event(s))")
        for ev in selected_events:
            res = render_event_zoom(
                ev, full_csv, charts_dir,
                pre_secs=args.pre, post_secs=args.post,
                quantities=zoom_qtys, image_format=args.format,
            )
            print(f"         event #{ev.id} {ev.kind}: "
                  f"{len(res.chart_paths)} chart(s)")
    else:
        print("[render] no events to zoom")

    if snaps:
        print(f"[render] snapshot zooms ({len(snaps)} snapshot(s))")
        for s in snaps:
            res = render_snapshot_zoom(
                s, full_csv, charts_dir,
                quantities=zoom_qtys, image_format=args.format,
            )
            print(f"         snapshot #{s.id}: {len(res.chart_paths)} chart(s)")

    if not args.no_xlsx:
        xlsx_path = outdir / "report.xlsx"
        print(f"[render] xlsx workbook → {xlsx_path}")
        write_xlsx(min_csv, xlsx_path, config=config, csv_per_second_path=full_csv)

    html_path = outdir / "report.html"
    if not args.no_html:
        print(f"[render] html report → {html_path}")
        # Reload insights from disk so plot-only flows still get them.
        insights_path = outdir / "insights.json"
        loaded_findings: list[Finding] = []
        if insights_path.exists():
            import json as _json
            for d in _json.loads(insights_path.read_text(encoding="utf-8")):
                loaded_findings.append(Finding(
                    id=d["id"], kind=d["kind"], severity=d["severity"],
                    headline=d["headline"], detail=d["detail"],
                    related_event_ids=tuple(d.get("related_event_ids", [])),
                    recommended_actions=tuple(d.get("recommended_actions", [])),
                ))
        write_html_report(
            html_path, charts_dir=charts_dir,
            config=config,
            summary_stats=_build_summary_stats(events, snaps),
            events=events, snapshots=snaps,
            findings=loaded_findings,
        )

    if args.pdf:
        pdf_path = outdir / "report.pdf"
        if not html_path.is_file():
            print(f"ERROR: --pdf requires the HTML report (don't combine with --no-html)",
                  file=sys.stderr)
            return
        print(f"[render] pdf report → {pdf_path}")
        try:
            write_pdf_report(html_path, pdf_path)
        except WeasyPrintNotInstalled as e:
            print(f"ERROR: {e}", file=sys.stderr)


def _load_existing(outdir: Path) -> tuple[Path, Path, list[Event], list[Snapshot], dict]:
    full_csv = outdir / "session.csv"
    min_csv = outdir / "session_1min.csv"
    events_path = outdir / "events.json"
    snaps_path = outdir / "snapshots.json"
    for p in (full_csv, min_csv, events_path):
        if not p.exists():
            raise SystemExit(f"--plot-only: required file missing: {p}")
    events = [_event_from_json(d) for d in json.loads(events_path.read_text())]
    snaps = []
    if snaps_path.exists():
        snaps = [
            Snapshot(
                id=d["id"], t_start=_parse_time(d["t_start"]),
                t_end=_parse_time(d["t_end"]),
                t_center=_parse_time(d["t_center"]),
                p_total_mean_w=d["p_total_mean_w"],
                p_total_stdev_w=d["p_total_stdev_w"],
            )
            for d in json.loads(snaps_path.read_text())
        ]
    # Synthesize a minimal config dict from the first row of session.csv.
    config: dict = {}
    return full_csv, min_csv, events, snaps, config


def _interactive(events: Sequence[Event], snaps: Sequence[Snapshot],
                 default_qtys: Sequence[str]) -> tuple[list[Event], list[str]]:
    """Lightweight plain-input picker. Returns (picked_events, picked_quantities)."""
    from .interactive import pick_events, pick_quantities
    picked_events = pick_events(events)
    picked_qtys = pick_quantities(default_qtys, list(FULL_QUANTITIES.keys()))
    return picked_events, picked_qtys


def _build_summary_stats(events: Sequence[Event], snaps: Sequence[Snapshot]) -> dict:
    by_kind: dict[str, int] = {}
    for ev in events:
        by_kind[ev.kind] = by_kind.get(ev.kind, 0) + 1
    return {
        "event_count": len(events),
        "events_by_kind": by_kind,
        "snapshot_count": len(snaps),
    }


def _emit_json(events: Sequence[Event], snaps: Sequence[Snapshot],
               findings: Sequence[Finding], config: dict) -> None:
    """Print a single JSON object to stdout. No trailing newline beyond json.dump's."""
    payload = {
        "config": config,
        "summary_stats": _build_summary_stats(events, snaps),
        "events": [_event_to_json(e) for e in events],
        "snapshots": [_snapshot_to_json(s) for s in snaps],
        "insights": [finding_to_jsonable(f) for f in findings],
    }
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    # Subcommand dispatch — `fluke-analyze compare ...` routes to cli_compare.
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "compare":
        from .cli_compare import compare_main
        return compare_main(raw_argv[1:])

    # Make console output UTF-8 safe on Windows (default cp1252 chokes on →, σ, etc.)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    args = build_argparser().parse_args(argv)

    # In --json mode, suppress every other stdout print so the output stays
    # a single valid JSON blob. We do this by replacing the module-level
    # print() with a no-op for the duration of this call.
    if getattr(args, "json_mode", False):
        global print
        _original_print = print
        def print(*a, **kw):  # noqa: A001 — intentional shadow
            kw.setdefault("file", sys.stderr)
            _original_print(*a, **kw)

    if not args.session_dir.exists():
        print(f"ERROR: {args.session_dir} does not exist", file=sys.stderr)
        return 1
    if not (args.session_dir.is_dir()
            or (args.session_dir.is_file()
                and args.session_dir.suffix.lower() in (".fel", ".csv"))):
        print(f"ERROR: {args.session_dir} is not a directory, a .fel, or a .csv",
              file=sys.stderr)
        return 1

    outdir = _resolve_outdir(args)
    outdir.mkdir(parents=True, exist_ok=True)

    try:
        if args.plot_only:
            full_csv, min_csv, events, snaps, config = _load_existing(outdir)
            _render_phase(args, outdir, full_csv, min_csv, events, snaps, config)
            return 0

        # Phase 1: parse + detect (open_session transparently unpacks .fel;
        # CSV inputs skip the binary parse entirely).
        if _is_csv_input(args.session_dir):
            full_csv, min_csv, config = _parse_csv_session(args, outdir)
            events, snaps, findings, store = _detect_and_save(
                args, outdir, trend_path=None, config=config, csv_path=full_csv,
            )
            _write_summary_txt(outdir, events, snaps, findings, config)
        else:
            with open_session(args.session_dir) as session_dir:
                full_csv, min_csv, config, store = _parse_session(
                    args, outdir, session_dir)
                events, snaps, findings, store = _detect_and_save(
                    args, outdir, trend_path=None, config=config, store=store)
                _write_summary_txt(outdir, events, snaps, findings, config)

        # Post-detection analysis features (markers, stats, tod, split) run on
        # the in-memory store + on-disk CSVs.
        _run_extra_analyses(args, outdir, store, events, findings, config,
                            full_csv, min_csv)

        if getattr(args, "json_mode", False):
            _emit_json(events, snaps, findings, config)
            return 0

        if args.parse_only:
            print(f"[done] parse-only: {outdir}")
            return 0

        if args.interactive:
            full_qtys_default = _parse_quantities(
                args.plot, DEFAULT_PLOTS, FULL_QUANTITIES.keys(),
            )
            picked_events, picked_qtys = _interactive(events, snaps, full_qtys_default)
            events = picked_events
            args.plot = ",".join(picked_qtys)

        _render_phase(args, outdir, full_csv, min_csv, events, snaps, config)
        print(f"[done] {outdir}")
        return 0

    except GnuplotNotFound as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
