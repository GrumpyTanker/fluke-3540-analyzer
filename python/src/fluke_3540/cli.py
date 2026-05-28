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

from .events import Event, detect_events
from .parser import export_csv, find_session_files, iter_records
from .plots import (
    GnuplotNotFound, render_event_zoom, render_full_session,
    render_snapshot_zoom, write_xlsx,
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
    ap.add_argument("session_dir", type=Path, help="Path to an ES.NNN session directory")
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
    ap.add_argument("--reverse-cts", action="store_true",
                    help="Negate P/Q/PF/DPF/Wh/VARh to correct backwards iFlex CTs")
    ap.add_argument("--every", type=int, default=1, metavar="K",
                    help="Emit every K-th record into the CSV (default 1, all)")

    # Output knobs
    ap.add_argument("--no-xlsx", action="store_true", help="Skip XLSX report")
    ap.add_argument("--no-overview", action="store_true",
                    help="Skip the overview multiplot")
    ap.add_argument("--format", choices=("png", "svg"), default="png",
                    help="Chart image format (default png)")
    ap.add_argument("--nominal-ln-v", type=float, default=None, metavar="V",
                    help="Nominal L-N voltage (auto-inferred if omitted)")

    return ap


def _resolve_outdir(args: argparse.Namespace) -> Path:
    if args.output:
        return args.output
    name = args.session_dir.name + "_out"
    return args.session_dir.parent / name


def _parse_session(args: argparse.Namespace, outdir: Path) -> tuple[Path, Path, dict]:
    """Phase 1A: parse trend.bin → session.csv + session_1min.csv."""
    full_csv = outdir / "session.csv"
    min_csv = outdir / "session_1min.csv"
    print(f"[parse] {args.session_dir}  →  {full_csv}")
    parse_result = export_csv(
        args.session_dir, full_csv,
        every=args.every, reverse_cts=args.reverse_cts,
    )
    print(f"        {parse_result['rows_written']:,} rows, {parse_result['columns']} cols")
    print(f"[parse] downsampling to 1-min  →  {min_csv}")
    export_csv(
        args.session_dir, min_csv,
        every=max(60, args.every), reverse_cts=args.reverse_cts,
    )
    return full_csv, min_csv, parse_result["config"]


def _detect_and_save(args: argparse.Namespace, outdir: Path,
                     trend_path: Path) -> tuple[list[Event], list[Snapshot]]:
    """Phase 1B: run event + snapshot detection, write JSON."""
    print("[detect] running event scan…")
    recs = list(iter_records(trend_path))
    if args.from_time or args.to_time:
        before = len(recs)
        recs = [
            r for r in recs
            if (not args.from_time or r.start >= args.from_time)
            and (not args.to_time or r.end <= args.to_time)
        ]
        print(f"         window filter: {before:,} → {len(recs):,} records")
    events = detect_events(recs, nominal_ln_v=args.nominal_ln_v)
    snaps = pick_snapshots(recs, events, n=args.snapshots)
    print(f"         {len(events)} events, {len(snaps)} snapshots")

    events_path = outdir / "events.json"
    snaps_path = outdir / "snapshots.json"
    events_path.write_text(json.dumps(
        [_event_to_json(e) for e in events], indent=2,
    ), encoding="utf-8")
    snaps_path.write_text(json.dumps(
        [_snapshot_to_json(s) for s in snaps], indent=2,
    ), encoding="utf-8")
    print(f"         wrote {events_path.name}, {snaps_path.name}")
    return events, snaps


def _write_summary_txt(outdir: Path, events: Sequence[Event],
                       snaps: Sequence[Snapshot], config: dict) -> None:
    lines: list[str] = ["Fluke 3540 FC Session Summary", "=" * 32, ""]
    if config:
        if config.get("asset_name"):
            lines.append(f"Asset:       {config['asset_name']}")
        if config.get("team_name"):
            lines.append(f"Team:        {config['team_name']}")
        if config.get("type"):
            lines.append(f"Instrument:  {config['type']}  fw={config.get('firmware_version', '?')}")
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


def main(argv: Sequence[str] | None = None) -> int:
    # Make console output UTF-8 safe on Windows (default cp1252 chokes on →, σ, etc.)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    args = build_argparser().parse_args(argv)

    if not args.session_dir.is_dir():
        print(f"ERROR: {args.session_dir} is not a directory", file=sys.stderr)
        return 1

    outdir = _resolve_outdir(args)
    outdir.mkdir(parents=True, exist_ok=True)

    try:
        if args.plot_only:
            full_csv, min_csv, events, snaps, config = _load_existing(outdir)
            _render_phase(args, outdir, full_csv, min_csv, events, snaps, config)
            return 0

        # Phase 1: parse + detect
        full_csv, min_csv, config = _parse_session(args, outdir)
        trend = find_session_files(args.session_dir)["trend"]
        events, snaps = _detect_and_save(args, outdir, trend)
        _write_summary_txt(outdir, events, snaps, config)

        if args.parse_only:
            print(f"[done] parse-only: {outdir}")
            return 0

        if args.interactive:
            full_qtys_default = _parse_quantities(
                args.plot, DEFAULT_PLOTS, FULL_QUANTITIES.keys(),
            )
            picked_events, picked_qtys = _interactive(events, snaps, full_qtys_default)
            # Replace event list and overwrite args.plot for the render call.
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
