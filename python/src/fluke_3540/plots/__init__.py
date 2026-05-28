"""Chart rendering — gnuplot driver, full-session plots, event zooms, XLSX, HTML, PDF."""
from .gnuplot import GnuplotNotFound, find_gnuplot, run_script
from .xlsx import write_xlsx
from .full_session import render_full_session
from .event_zoom import render_event_zoom, render_snapshot_zoom
from .html_report import write_html_report, render_report_html
from .pdf_report import WeasyPrintNotInstalled, write_pdf_report

__all__ = [
    "GnuplotNotFound", "find_gnuplot", "run_script",
    "write_xlsx",
    "render_full_session", "render_event_zoom", "render_snapshot_zoom",
    "write_html_report", "render_report_html",
    "write_pdf_report", "WeasyPrintNotInstalled",
]
