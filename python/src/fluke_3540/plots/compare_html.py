"""Self-contained HTML report for multi-session compare runs.

Layout: cover with per-session summary table, cross-session findings,
overlay chart figures (one per quantity). Pairs with the single-session
``html_report.py`` template — same CSS, same severity color scheme.
"""
from __future__ import annotations

import base64
import datetime as dt
import html
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from ..insights_compare import CompareFinding


# Reuse the CSS from html_report.py so the two report styles match.
from .html_report import _CSS as _SINGLE_CSS


_CSS = _SINGLE_CSS + """
.compare-summary table { margin-bottom: 1rem; }
.compare-summary th, .compare-summary td { text-align: right; }
.compare-summary th:first-child, .compare-summary td:first-child {
  text-align: left; font-weight: bold;
}
"""


def _summary_table_html(session_stats: Sequence[Mapping]) -> str:
    """Render per-session summary as a side-by-side table."""
    if not session_stats:
        return ""
    metrics = [
        ("rows",            "Records"),
        ("imported_kwh",    "Imported (kWh)"),
        ("exported_kwh",    "Exported (kWh)"),
        ("peak_import_kw",  "Peak import (kW)"),
        ("peak_export_kw",  "Peak export (kW)"),
        ("peak_current_a",  "Peak current (A)"),
    ]
    rows: list[str] = []
    rows.append(
        "<thead><tr><th>Metric</th>"
        + "".join(f"<th>{html.escape(s['label'])}</th>" for s in session_stats)
        + "</tr></thead>"
    )
    body: list[str] = []
    for key, label in metrics:
        cells = [f"<td>{html.escape(label)}</td>"]
        for s in session_stats:
            v = s.get(key)
            if isinstance(v, (int, float)):
                if key == "rows":
                    cells.append(f"<td>{int(v):,}</td>")
                else:
                    cells.append(f"<td>{v:.3f}</td>")
            else:
                cells.append("<td></td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    rows.append("<tbody>" + "\n".join(body) + "</tbody>")
    return "<div class='compare-summary'><table>" + "".join(rows) + "</table></div>"


def _insights_html(findings: Sequence[CompareFinding]) -> str:
    if not findings:
        return ""
    out = ["<h2>Cross-session insights</h2>"]
    for f in findings:
        actions = "".join(
            f"<li>{html.escape(a)}</li>" for a in f.recommended_actions
        )
        actions_block = (
            f"<p class='meta'>Recommended</p><ul>{actions}</ul>" if actions else ""
        )
        out.append(
            f"<section class='insight {html.escape(f.severity)}'>"
            f"<h3>{html.escape(f.headline)}</h3>"
            f"<p class='meta'>{html.escape(f.kind)} · {html.escape(f.severity)}"
            f" · sessions: {html.escape(', '.join(f.session_labels))}</p>"
            f"<p>{html.escape(f.detail)}</p>"
            f"{actions_block}"
            "</section>"
        )
    return "\n".join(out)


def _chart_figures_html(charts: Iterable[tuple[str, bytes]]) -> str:
    out: list[str] = []
    if not charts:
        return ""
    out.append("<h2>Overlay charts</h2>")
    for name, data in charts:
        b64 = base64.b64encode(data).decode("ascii")
        title = Path(name).stem.replace("compare_", "").replace("_", " ").title()
        out.append(
            f'<figure><img src="data:image/png;base64,{b64}" alt="{html.escape(title)}">'
            f"<figcaption>{html.escape(title)}</figcaption></figure>"
        )
    return "\n".join(out)


def render_compare_html(
    *,
    title: str,
    session_stats: Sequence[Mapping],
    findings: Sequence[CompareFinding],
    charts: Iterable[tuple[str, bytes]],
    generated_at: dt.datetime | None = None,
) -> str:
    generated_at = generated_at or dt.datetime.now(dt.timezone.utc)
    body = [
        f"<h1>{html.escape(title)}</h1>",
        "<h2>Per-session summary</h2>",
        _summary_table_html(session_stats),
    ]
    if findings:
        body.append(_insights_html(findings))
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


def write_compare_html_report(
    output_path: Path, *,
    output_dir: Path,
    session_stats: Sequence[Mapping],
    findings: Sequence[CompareFinding],
    title: str | None = None,
) -> Path:
    """Find compare_*.png in output_dir, base64-embed, write self-contained HTML."""
    if title is None:
        title = "Fluke 3540 FC — Multi-session Comparison"
    charts: list[tuple[str, bytes]] = []
    if output_dir.is_dir():
        for p in sorted(output_dir.glob("compare_*.png")):
            charts.append((p.name, p.read_bytes()))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_compare_html(
            title=title, session_stats=session_stats,
            findings=findings, charts=charts,
        ),
        encoding="utf-8",
    )
    return output_path
