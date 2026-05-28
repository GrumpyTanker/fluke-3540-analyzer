"""PDF report generation — thin wrapper around weasyprint.

weasyprint is an optional dependency (``pip install fluke-3540-analyzer[pdf]``).
The HTML report from ``html_report.py`` already has print-friendly CSS via
``@media print``; weasyprint renders it to PDF cleanly.

If weasyprint isn't installed, ``write_pdf_report`` raises ``WeasyPrintNotInstalled``
with an actionable message. The CLI catches this and prints the install hint.
"""
from __future__ import annotations

from pathlib import Path


class WeasyPrintNotInstalled(ImportError):
    """Raised when --pdf is requested but weasyprint isn't available."""


def write_pdf_report(html_path: Path, output_path: Path) -> Path:
    """Render an existing HTML report to a PDF using weasyprint.

    Args:
        html_path: existing report.html produced by ``write_html_report``.
        output_path: where to write the PDF.
    """
    # Check the HTML exists first so a missing input wins over a missing dep.
    if not html_path.is_file():
        raise FileNotFoundError(
            f"HTML report not found: {html_path}. The --pdf flag depends on "
            "the HTML report being generated first (don't combine with --no-html)."
        )
    try:
        from weasyprint import HTML  # type: ignore
    except ImportError as e:
        raise WeasyPrintNotInstalled(
            "weasyprint not installed. Install with:\n"
            "    pip install 'fluke-3540-analyzer[pdf]'\n"
            "Alternatively, open report.html in a browser and use the "
            "browser's Print → Save as PDF dialog."
        ) from e
    output_path.parent.mkdir(parents=True, exist_ok=True)
    HTML(filename=str(html_path)).write_pdf(str(output_path))
    return output_path
