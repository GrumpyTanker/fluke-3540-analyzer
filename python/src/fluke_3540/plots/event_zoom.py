"""Event-zoom and snapshot-zoom charts — narrow time-window views per event/snapshot."""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from ..events import Event
from ..snapshots import Snapshot
from .gnuplot import gnuplot_path_str, run_script


# Quantity → list of (csv_column_name, label, color) for event zooms.
# Uses min/max columns where appropriate (catches sub-second behavior).
ZOOM_QUANTITIES: dict[str, tuple[str, str, list[tuple[str, str, str]]]] = {
    "voltage": ("Per-phase L-N Voltage (window MIN)", "V (V)",
                [("V_LN_a_min_V", "V_{LN,a} min", "#cc0000"),
                 ("V_LN_b_min_V", "V_{LN,b} min", "#0066cc"),
                 ("V_LN_c_min_V", "V_{LN,c} min", "#009933")]),
    "current": ("Per-phase Current (window MAX)", "I peak (A)",
                [("I_a_max_A", "I_a max", "#cc0000"),
                 ("I_b_max_A", "I_b max", "#0066cc"),
                 ("I_c_max_A", "I_c max", "#009933")]),
    "power":   ("Active Power", "P (kW)",
                [("P_total_avg_W", "P_{total}", "#cc0000")]),
    "pf":      ("Power Factor", "PF",
                [("PF_total_avg", "PF", "#cc0000")]),
    "frequency": ("Line Frequency", "Frequency (Hz)",
                  [("freq_avg_Hz", "f", "#660066")]),
}

# These columns are scaled before plotting (e.g. W → kW).
SCALES = {"P_total_avg_W": 1e-3}


@dataclass
class ZoomResult:
    tsv_path: Path
    script_path: Path
    chart_paths: list[Path]


def _write_tsv_filtered(
    csv_path: Path, tsv_path: Path, columns: Sequence[str],
    t_min: dt.datetime, t_max: dt.datetime,
) -> int:
    """Write a TSV containing only rows within [t_min, t_max]. Returns row count."""
    rows = 0
    with csv_path.open(newline="") as src, tsv_path.open("w", newline="") as dst:
        reader = csv.DictReader(src)
        dst.write("epoch_s")
        for c in columns:
            dst.write("\t" + c)
        dst.write("\n")
        for r in reader:
            try:
                t = dt.datetime.fromisoformat(r["timestamp_utc"])
            except (KeyError, ValueError):
                continue
            if t < t_min or t > t_max:
                continue
            parts = [f"{t.timestamp():.0f}"]
            for c in columns:
                raw = r.get(c, "")
                if raw == "":
                    parts.append("NaN")
                    continue
                try:
                    v = float(raw) * SCALES.get(c, 1.0)
                    parts.append(f"{v:.6g}")
                except ValueError:
                    parts.append("NaN")
            dst.write("\t".join(parts) + "\n")
            rows += 1
    return rows


def _zoom_script(
    tsv_path: Path, charts_dir: Path, label_prefix: str,
    window_title: str, t_min: dt.datetime, t_max: dt.datetime,
    quantities: Sequence[str], image_format: str,
) -> tuple[str, list[Path], list[str]]:
    """Build the gnuplot script lines for one zoom window. Returns (script_text, output_paths, used_cols)."""
    tsv_gp = gnuplot_path_str(tsv_path)
    charts_gp = gnuplot_path_str(charts_dir)

    used_cols: list[str] = []
    for q in quantities:
        for col, _, _ in ZOOM_QUANTITIES[q][2]:
            if col not in used_cols:
                used_cols.append(col)
    col_index = {name: i + 2 for i, name in enumerate(used_cols)}

    epoch_min = int(t_min.timestamp())
    epoch_max = int(t_max.timestamp())
    terminal = (
        "set terminal pngcairo size 1600,700 enhanced font 'Segoe UI,11' "
        "background '#ffffff'"
        if image_format == "png" else
        "set terminal svg size 1600,700 enhanced font 'Segoe UI,11' "
        "background '#ffffff'"
    )
    lines = [
        "set datafile separator '\\t'",
        "set datafile missing 'NaN'",
        "set xdata time",
        "set timefmt '%s'",
        "set format x '%H:%M:%S'",
        "set xtics rotate by -30 font ',9'",
        "set grid xtics ytics linecolor rgb '#cccccc'",
        "set border linecolor rgb '#666666'",
        "set key outside right top box",
        "set tics nomirror",
        terminal,
        "",
    ]

    out_paths: list[Path] = []
    for q in quantities:
        title, ylabel, specs = ZOOM_QUANTITIES[q]
        png = f"{label_prefix}_{q}.{image_format}"
        out_paths.append(charts_dir / png)
        lines.append(f"# --- {png}")
        lines.append(f"set output '{charts_gp}/{png}'")
        lines.append(f"set title '{window_title} — {title}' font ',13'")
        lines.append("set xlabel 'Time (UTC)'")
        lines.append(f"set ylabel '{ylabel}'")
        lines.append(f"set xrange [{epoch_min}:{epoch_max}]")
        plots = [
            f"'{tsv_gp}' using 1:{col_index[col]} with linespoints "
            f"linewidth 1.5 pointtype 7 pointsize 0.4 "
            f"linecolor rgb '{color}' title '{label}'"
            for col, label, color in specs
        ]
        lines.append("plot " + ", \\\n     ".join(plots))
        lines.append("")

    return "\n".join(lines), out_paths, used_cols


def render_event_zoom(
    event: Event, csv_path: Path, output_dir: Path,
    pre_secs: int = 30, post_secs: int = 60,
    quantities: Iterable[str] = ("voltage", "current", "power"),
    image_format: str = "png",
    gnuplot: Path | None = None,
) -> ZoomResult:
    """Render zoom charts for a single Event."""
    quantities = list(quantities)
    bad = [q for q in quantities if q not in ZOOM_QUANTITIES]
    if bad:
        raise ValueError(f"Unknown zoom quantity/quantities: {bad}. "
                         f"Valid: {sorted(ZOOM_QUANTITIES)}")
    output_dir.mkdir(parents=True, exist_ok=True)

    t_min = event.t_start - dt.timedelta(seconds=pre_secs)
    t_max = event.t_end + dt.timedelta(seconds=post_secs)
    label_prefix = f"event_{event.id:03d}_{event.kind}"
    window_title = (
        f"Event #{event.id} {event.kind.upper()} "
        f"({event.t_start.isoformat()}, {len(event.affected_phases) or 0} phases)"
    )

    tsv_path = output_dir / f"{label_prefix}.tsv"
    script_path = output_dir / f"{label_prefix}.gp"
    script_text, chart_paths, used_cols = _zoom_script(
        tsv_path, output_dir, label_prefix, window_title, t_min, t_max,
        quantities, image_format,
    )
    _write_tsv_filtered(csv_path, tsv_path, used_cols, t_min, t_max)
    script_path.write_text(script_text, encoding="utf-8")
    run_script(script_path, gnuplot=gnuplot)
    return ZoomResult(
        tsv_path=tsv_path, script_path=script_path, chart_paths=chart_paths,
    )


def render_snapshot_zoom(
    snapshot: Snapshot, csv_path: Path, output_dir: Path,
    quantities: Iterable[str] = ("voltage", "current", "power"),
    image_format: str = "png",
    gnuplot: Path | None = None,
) -> ZoomResult:
    """Render zoom charts for one normal-operation Snapshot window."""
    quantities = list(quantities)
    bad = [q for q in quantities if q not in ZOOM_QUANTITIES]
    if bad:
        raise ValueError(f"Unknown zoom quantity/quantities: {bad}. "
                         f"Valid: {sorted(ZOOM_QUANTITIES)}")
    output_dir.mkdir(parents=True, exist_ok=True)

    label_prefix = f"snapshot_{snapshot.id:03d}"
    window_title = (
        f"Snapshot #{snapshot.id} "
        f"({snapshot.t_start.isoformat()} +{int((snapshot.t_end - snapshot.t_start).total_seconds())}s)"
    )

    tsv_path = output_dir / f"{label_prefix}.tsv"
    script_path = output_dir / f"{label_prefix}.gp"
    script_text, chart_paths, used_cols = _zoom_script(
        tsv_path, output_dir, label_prefix, window_title,
        snapshot.t_start, snapshot.t_end, quantities, image_format,
    )
    _write_tsv_filtered(csv_path, tsv_path, used_cols,
                        snapshot.t_start, snapshot.t_end)
    script_path.write_text(script_text, encoding="utf-8")
    run_script(script_path, gnuplot=gnuplot)
    return ZoomResult(
        tsv_path=tsv_path, script_path=script_path, chart_paths=chart_paths,
    )
