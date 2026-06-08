"""Self-contained HTML report — base64-embedded chart PNGs + summary + events table.

The output is a single .html file that opens in any browser with no network
requests. Designed to be shareable as an attachment.

CLI wiring sets `--no-html` to opt out; otherwise the default is to write
``report.html`` next to the other artifacts.
"""
from __future__ import annotations

import base64
import datetime as dt
import html
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from ..events import Event
from ..insights import Finding
from ..snapshots import Snapshot


# Chart-filename → human label heuristic. We let the filename pattern from
# full_session.py / event_zoom.py drive the section order in the report.
_CHART_SECTION_ORDER = ("overview", "full", "event", "snapshot")


def _classify_chart(name: str) -> tuple[str, str]:
    """Return (section_key, display_title) for a chart filename.

    Filenames produced by v0.1 plots:
        overview.png
        full_<quantity>.{png,svg}
        event_<id>_<kind>_<quantity>.{png,svg}
        snapshot_<id>_<quantity>.{png,svg}
    """
    stem = Path(name).stem
    if stem == "overview":
        return ("overview", "Session overview")
    if stem.startswith("full_"):
        return ("full", "Full session — " + stem[len("full_"):])
    if stem.startswith("event_"):
        # event_009_dip_voltage → "Event 9 dip — voltage"
        parts = stem.split("_", 3)
        if len(parts) >= 4:
            return ("event", f"Event {int(parts[1])} {parts[2]} — {parts[3]}")
        return ("event", stem)
    if stem.startswith("snapshot_"):
        parts = stem.split("_", 2)
        if len(parts) >= 3:
            return ("snapshot", f"Snapshot {int(parts[1])} — {parts[2]}")
        return ("snapshot", stem)
    return ("other", stem)


_CSS = """\
:root { color-scheme: light dark; }
body { font-family: system-ui, sans-serif; max-width: 1100px; margin: 2rem auto;
       padding: 0 1rem; line-height: 1.5; color: #222; background: #fff; }
@media (prefers-color-scheme: dark) {
  body { color: #ddd; background: #1a1a1a; }
  th { background: #2a2a2a; }
  td { border-top-color: #333; }
  pre { background: #2a2a2a; }
}
h1 { font-size: 1.5rem; margin-top: 0; }
h2 { margin-top: 2rem; border-bottom: 1px solid #ccc; padding-bottom: 0.25rem; }
h3 { margin-top: 1.5rem; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 0.3rem 1.5rem;
     font-variant-numeric: tabular-nums; }
dt { font-weight: bold; }
dd { margin: 0; }
table { border-collapse: collapse; width: 100%; font-size: 0.9rem;
        font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 0.35rem 0.5rem; }
th { background: #f4f4f4; border-bottom: 2px solid #ccc; }
td { border-top: 1px solid #eee; }
figure { margin: 1rem 0; }
figure img { max-width: 100%; height: auto; display: block; border: 1px solid #ddd; }
figcaption { font-size: 0.85rem; color: #666; margin-top: 0.3rem; }
footer { margin-top: 3rem; color: #888; font-size: 0.85rem;
         border-top: 1px solid #ccc; padding-top: 1rem; }
.insight { border-left: 4px solid #888; padding: 0.5rem 1rem;
           margin: 0.75rem 0; background: rgba(0, 0, 0, 0.025); }
.insight.alert { border-left-color: #cc0000; }
.insight.warn  { border-left-color: #cc6600; }
.insight.info  { border-left-color: #0066cc; }
.insight h3 { margin: 0 0 0.25rem 0; font-size: 1rem; }
.insight .meta { color: #888; font-size: 0.8rem; text-transform: uppercase; }
.insight ul { margin: 0.25rem 0 0 1.2rem; }
@media print { body { max-width: none; } h2 { page-break-before: always; } }
"""


def _summary_dl_html(stats: Mapping[str, str | int | float],
                     config: Mapping[str, str] | None) -> str:
    rows: list[tuple[str, str]] = []
    if config:
        for k_disp, k_cfg in (
            ("Asset", "asset_name"),
            ("Team", "team_name"),
            ("Instrument", "type"),
            ("Firmware", "firmware_version"),
        ):
            v = config.get(k_cfg)
            if v:
                rows.append((k_disp, str(v)))
    for k, v in stats.items():
        rows.append((k, str(v)))
    return "<dl>\n" + "\n".join(
        f"  <dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd>"
        for k, v in rows
    ) + "\n</dl>"


def _events_table_html(events: Sequence[Event]) -> str:
    if not events:
        return "<p><em>No events detected.</em></p>"
    rows: list[str] = []
    for ev in events:
        phases = "/".join(ev.affected_phases) or "—"
        rows.append(
            f"<tr><td>{ev.id}</td><td>{html.escape(ev.kind)}</td>"
            f"<td>{html.escape(ev.t_start.isoformat())}</td>"
            f"<td>{int(max(1, (ev.t_end - ev.t_start).total_seconds()))} s</td>"
            f"<td>{html.escape(phases)}</td>"
            f"<td>{ev.severity:.3f}</td></tr>"
        )
    return (
        "<table>\n"
        "<thead><tr><th>ID</th><th>Kind</th><th>Start (UTC)</th>"
        "<th>Duration</th><th>Phases</th><th>Severity</th></tr></thead>\n"
        "<tbody>\n" + "\n".join(rows) + "\n</tbody></table>"
    )


def _snapshots_table_html(snaps: Sequence[Snapshot]) -> str:
    if not snaps:
        return ""
    rows = [
        f"<tr><td>{s.id}</td><td>{html.escape(s.t_start.isoformat())}</td>"
        f"<td>{s.p_total_mean_w / 1000:+.2f} kW</td>"
        f"<td>{s.p_total_stdev_w:.1f} W</td></tr>"
        for s in snaps
    ]
    return (
        "<h3>Quiet snapshots</h3>\n<table>\n"
        "<thead><tr><th>ID</th><th>Start (UTC)</th>"
        "<th>Mean P</th><th>σ(P)</th></tr></thead>\n"
        "<tbody>\n" + "\n".join(rows) + "\n</tbody></table>"
    )


def _chart_figures_html(charts: Iterable[tuple[str, bytes]]) -> str:
    """charts is an iterable of (filename, png_bytes). Groups by section."""
    by_section: dict[str, list[tuple[str, bytes]]] = {}
    for name, data in charts:
        section, _ = _classify_chart(name)
        by_section.setdefault(section, []).append((name, data))
    out: list[str] = []
    for section in _CHART_SECTION_ORDER:
        entries = by_section.get(section)
        if not entries:
            continue
        heading = {
            "overview": "Overview",
            "full":     "Full-session charts",
            "event":    "Event zooms",
            "snapshot": "Snapshot zooms",
        }[section]
        out.append(f"<h2>{heading}</h2>")
        for name, data in entries:
            _, title = _classify_chart(name)
            b64 = base64.b64encode(data).decode("ascii")
            out.append(
                f'<figure><img src="data:image/png;base64,{b64}" alt="{html.escape(title)}">'
                f"<figcaption>{html.escape(title)}</figcaption></figure>"
            )
    return "\n".join(out)


def _insights_html(findings: Sequence[Finding]) -> str:
    if not findings:
        return ""
    out = ["<h2>Insights</h2>"]
    for f in findings:
        actions = "".join(
            f"<li>{html.escape(a)}</li>" for a in f.recommended_actions
        )
        actions_block = f"<p class='meta'>Recommended</p><ul>{actions}</ul>" if actions else ""
        out.append(
            f"<section class='insight {html.escape(f.severity)}'>"
            f"<h3>{html.escape(f.headline)}</h3>"
            f"<p class='meta'>{html.escape(f.kind)} · {html.escape(f.severity)}</p>"
            f"<p>{html.escape(f.detail)}</p>"
            f"{actions_block}"
            "</section>"
        )
    return "\n".join(out)


def _load_states_html(load_states: Mapping | None) -> str:
    """Compact active-vs-standby load-state table + the three energy figures."""
    if not load_states or not load_states.get("states"):
        return ""
    thr = load_states.get("standby_threshold_a", 50.0)
    rows = load_states["states"]
    parts = [
        f"<section class='load-states'><h2>Load states "
        f"(active vs standby, cut at {thr:.0f} A/phase)</h2>",
        "<table><thead><tr>"
        "<th>state</th><th>duty %</th><th>records</th><th>I avg (A)</th>"
        "<th>P avg (kW)</th><th>S avg (kVA)</th><th>PF</th><th>kWh</th>"
        "</tr></thead><tbody>",
    ]
    for r in rows:
        parts.append(
            "<tr>"
            f"<td>{html.escape(str(r['state']))}</td>"
            f"<td>{r['duty_pct']:.1f}</td>"
            f"<td>{r['records']}</td>"
            f"<td>{r['I_avg_A']:.1f}</td>"
            f"<td>{r['P_avg_kW']:+.2f}</td>"
            f"<td>{r['S_avg_kVA']:.2f}</td>"
            f"<td>{r['PF_avg']:+.3f}</td>"
            f"<td>{r['kWh']:.2f}</td>"
            "</tr>"
        )
    parts.append("</tbody></table>")
    en = load_states.get("energy")
    if en:
        parts.append(
            "<table class='energy'><thead><tr><th>energy figure</th>"
            "<th>kWh</th></tr></thead><tbody>"
            f"<tr><td>as-measured (signed)</td><td>"
            f"{en['energy_as_measured_kWh']:.1f}</td></tr>"
            f"<tr><td>active-only</td><td>{en['energy_active_kWh']:.1f}</td></tr>"
            f"<tr><td>net (standby clipped &ge;0)</td><td>"
            f"{en['energy_net_clip_standby_kWh']:.1f}</td></tr>"
            "</tbody></table>"
            "<p class='caveat'>Standby real-power sign is unreliable at low "
            "current, so the active / clip figures are the defensible "
            "consumption.</p>"
        )
    parts.append("</section>")
    return "".join(parts)


def render_report_html(
    *,
    title: str,
    config: Mapping[str, str] | None,
    summary_stats: Mapping[str, str | int | float],
    events: Sequence[Event],
    snapshots: Sequence[Snapshot],
    charts: Iterable[tuple[str, bytes]],
    findings: Sequence[Finding] = (),
    generated_at: dt.datetime | None = None,
    narrative: str | None = None,
    load_states: Mapping | None = None,
) -> str:
    generated_at = generated_at or dt.datetime.now(dt.timezone.utc)
    body = []
    body.append(f"<h1>{html.escape(title)}</h1>")
    if narrative:
        body.append(
            "<section class='narrative'><h2>Executive summary</h2><p>"
            + html.escape(narrative).replace("\n", "<br>")
            + "</p></section>"
        )
    body.append("<h2>Summary</h2>")
    body.append(_summary_dl_html(summary_stats, config))
    ls_html = _load_states_html(load_states)
    if ls_html:
        body.append(ls_html)
    if findings:
        body.append(_insights_html(findings))
    body.append("<h2>Events</h2>")
    body.append(_events_table_html(events))
    if snapshots:
        body.append(_snapshots_table_html(snapshots))
    body.append(_chart_figures_html(charts))
    body.append(
        f"<footer>Generated {html.escape(generated_at.isoformat())} by "
        "<a href='https://github.com/GrumpyTanker/fluke-3540-analyzer'>"
        "fluke-3540-analyzer</a></footer>"
    )
    return (
        "<!DOCTYPE html>\n"
        "<html lang='en'><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        f"<style>{_CSS}</style></head><body>\n"
        + "\n".join(body)
        + "\n</body></html>\n"
    )


def write_html_report(
    output_path: Path, *,
    charts_dir: Path,
    config: Mapping[str, str] | None,
    summary_stats: Mapping[str, str | int | float],
    events: Sequence[Event],
    snapshots: Sequence[Snapshot],
    findings: Sequence[Finding] = (),
    title: str | None = None,
    narrative: str | None = None,
    load_states: Mapping | None = None,
) -> Path:
    """High-level wrapper: read PNGs from charts_dir, write a self-contained HTML report.

    Only `.png` files are embedded. SVGs are skipped (HTML report is bitmap-only
    for max portability).
    """
    if title is None:
        asset = (config or {}).get("asset_name", "Session")
        title = f"Fluke 3540 FC — {asset} Report"
    charts: list[tuple[str, bytes]] = []
    if charts_dir.is_dir():
        for p in sorted(charts_dir.glob("*.png")):
            charts.append((p.name, p.read_bytes()))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_report_html(
            title=title, config=config, summary_stats=summary_stats,
            events=events, snapshots=snapshots, charts=charts,
            findings=findings, narrative=narrative, load_states=load_states,
        ),
        encoding="utf-8",
    )
    return output_path
