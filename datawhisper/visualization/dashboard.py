"""Plotly dark-mode dashboard and standalone executive report generator.

The module produces a single self-contained HTML file: KPI cards are plain
CSS (crisper than chart-based indicators at small sizes), while timelines,
benchmarks, drift and data distributions are Plotly figures rendered into
the same dark template. Plotly.js is loaded once from a CDN tag injected in
the page head, so each figure embed costs only its own JSON payload.
"""

from __future__ import annotations

from collections.abc import Sequence

import plotly.graph_objects as go
from pydantic import BaseModel

from datawhisper.core.agent import AgentStep
from datawhisper.core.database import CatalogSnapshot
from datawhisper.integrations.github_bot import BenchmarkResult, DriftReport

_BG = "#0d1117"
_CARD = "#161b22"
_BORDER = "#30363d"
_TEXT = "#c9d1d9"
_MUTED = "#8b949e"
_ACCENT = "#58a6ff"
_COLORS = ["#58a6ff", "#3fb950", "#d29922", "#f97583", "#bc8cff", "#39c5cf"]


class ReportSection(BaseModel):
    """One vertical block of the report; ``html`` may embed figure fragments."""

    heading: str
    html: str = ""


class KpiCard(BaseModel):
    label: str
    value: str
    detail: str = ""


def _dark_layout(title: str) -> go.Layout:
    return go.Layout(
        template="plotly_dark",
        title={"text": title, "font": {"color": _TEXT}},
        paper_bgcolor=_BG,
        plot_bgcolor=_BG,
        font={"color": _TEXT},
        margin={"l": 40, "r": 24, "t": 48, "b": 40},
    )


def _figure_html(figure: go.Figure, height: int = 360) -> str:
    figure.update_layout(height=height)
    return str(figure.to_html(full_html=False, include_plotlyjs=False))


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------
def table_rowcount_figure(catalog: CatalogSnapshot) -> go.Figure:
    """Rows per table; the first thing an analyst wants to see."""
    figure = go.Figure()
    names = [t.name for t in catalog.tables]
    counts = [t.row_count or 0 for t in catalog.tables]
    figure.add_trace(go.Bar(x=names, y=counts, marker_color=_ACCENT, name="rows"))
    figure.update_layout(_dark_layout("Rows per table"))
    return figure


def timeline_figure(steps: Sequence[AgentStep]) -> go.Figure:
    """Horizontal execution timeline of the ReAct loop (turn latency)."""
    figure = go.Figure()
    labels = [f"#{s.index} {s.action}" for s in steps][::-1]
    durations = [s.elapsed_ms for s in steps][::-1]
    figure.add_trace(
        go.Bar(
            orientation="h",
            x=durations,
            y=labels,
            marker_color=[_COLORS[s.index % len(_COLORS)] for s in steps][::-1],
            hovertext=[s.observation[:120] for s in steps][::-1],
            name="latency",
        )
    )
    figure.update_layout(_dark_layout("Agent execution timeline (ms per turn)"))
    figure.update_xaxes(title_text="elapsed ms", gridcolor=_BORDER)
    return figure


def drift_figure(drift: DriftReport) -> go.Figure:
    """Stacked added/removed/changed column counts per drifted table."""
    figure = go.Figure()
    tables = [t.table for t in drift.tables]
    figure.add_trace(
        go.Bar(
            name="added",
            x=tables,
            y=[len(t.added_columns) for t in drift.tables],
            marker_color="#3fb950",
        )
    )
    figure.add_trace(
        go.Bar(
            name="removed",
            x=tables,
            y=[len(t.removed_columns) for t in drift.tables],
            marker_color="#f97583",
        )
    )
    figure.add_trace(
        go.Bar(
            name="changed",
            x=tables,
            y=[len(t.type_changes) for t in drift.tables],
            marker_color="#d29922",
        )
    )
    figure.update_layout(_dark_layout("Schema drift by table"), barmode="stack")
    return figure


def benchmark_figure(benchmarks: Sequence[BenchmarkResult]) -> go.Figure:
    """Baseline vs candidate latency per benchmarked query."""
    figure = go.Figure()
    labels = [b.label for b in benchmarks]
    figure.add_trace(
        go.Bar(
            name="baseline",
            x=labels,
            y=[b.baseline_ms or 0 for b in benchmarks],
            marker_color=_ACCENT,
        )
    )
    figure.add_trace(
        go.Bar(
            name="candidate",
            x=labels,
            y=[b.candidate_ms or 0 for b in benchmarks],
            marker_color=[("#f97583" if b.regression else "#3fb950") for b in benchmarks],
        )
    )
    figure.update_layout(_dark_layout("Query benchmarks (ms, lower is better)"), barmode="group")
    figure.update_yaxes(title_text="ms", gridcolor=_BORDER)
    return figure


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------
def _kpi_grid(cards: Sequence[KpiCard]) -> str:
    parts: list[str] = []
    for card in cards:
        detail = f'<div class="detail">{card.detail}</div>' if card.detail else ""
        parts.append(
            f'<div class="card"><div class="label">{card.label}</div>'
            f'<div class="value">{card.value}</div>{detail}</div>'
        )
    return f'<div class="grid">{"".join(parts)}</div>'


_HEAD = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DataWhisper Report</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
<style>
  body {{ background: {_BG}; color: {_TEXT}; margin: 0;
         font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; }}
  .wrap {{ max-width: 1080px; margin: 0 auto; padding: 32px 20px 64px; }}
  h1 {{ color: {_ACCENT}; font-size: 26px; margin: 0 0 4px; }}
  h2 {{ color: {_TEXT}; font-size: 18px; border-bottom: 1px solid {_BORDER};
        padding-bottom: 8px; margin-top: 40px; }}
  .meta {{ color: {_MUTED}; font-size: 13px; margin-bottom: 24px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
           gap: 16px; }}
  .card {{ background: {_CARD}; border: 1px solid {_BORDER}; border-radius: 12px;
           padding: 18px; }}
  .label {{ color: {_MUTED}; font-size: 11px; text-transform: uppercase;
            letter-spacing: 0.08em; }}
  .value {{ font-size: 28px; font-weight: 700; color: {_ACCENT}; margin-top: 6px; }}
  .detail {{ color: {_MUTED}; font-size: 12px; margin-top: 4px; }}
  .insights {{ background: {_CARD}; border: 1px solid {_BORDER}; border-radius: 12px;
               padding: 16px 20px; line-height: 1.6; white-space: pre-wrap; }}
  a {{ color: {_ACCENT}; }}
</style>
</head>
<body>
<div class="wrap">
"""


def render_report_html(
    title: str,
    subtitle: str = "",
    sections: Sequence[ReportSection] = (),
    kpis: Sequence[KpiCard] = (),
) -> str:
    """Assemble the standalone dark-mode dashboard HTML."""
    parts = [_HEAD]
    parts.append(f"<h1>{title}</h1>")
    if subtitle:
        parts.append(f'<div class="meta">{subtitle}</div>')
    if kpis:
        parts.append(_kpi_grid(kpis))
    for section in sections:
        parts.append(f"<h2>{section.heading}</h2>")
        if section.html:
            parts.append(section.html)
    parts.append("</div></body></html>\n")
    return "".join(parts)


def render_audit_html(
    *,
    drift: DriftReport | None = None,
    benchmarks: Sequence[BenchmarkResult] | None = None,
    insights: str | None = None,
    timeline_steps: Sequence[AgentStep] = (),
    title: str = "DataWhisper Audit",
    subtitle: str = "",
) -> str:
    """Convenience builder wiring an audit result into the dark template."""
    kpis: list[KpiCard] = []
    sections: list[ReportSection] = []

    if drift is not None:
        changed_columns = sum(
            len(t.added_columns) + len(t.removed_columns) + len(t.type_changes)
            for t in drift.tables
        )
        kpis.append(KpiCard(label="Drifted tables", value=str(len(drift.tables))))
        kpis.append(KpiCard(label="Column changes", value=str(changed_columns)))
        kpis.append(KpiCard(label="New tables", value=str(len(drift.added_tables))))
        kpis.append(KpiCard(label="Dropped tables", value=str(len(drift.dropped_tables))))
        if drift.has_changes:
            sections.append(
                ReportSection(heading="Schema drift", html=_figure_html(drift_figure(drift)))
            )

    if benchmarks:
        regressions = sum(1 for b in benchmarks if b.regression)
        total_delta = sum(b.delta_ms or 0.0 for b in benchmarks)
        kpis.append(KpiCard(label="Regressions", value=str(regressions)))
        kpis.append(
            KpiCard(
                label="Latency delta",
                value=f"{total_delta:+.1f} ms",
                detail="across benchmark suite",
            )
        )
        sections.append(
            ReportSection(
                heading="Query benchmarks", html=_figure_html(benchmark_figure(benchmarks))
            )
        )

    if timeline_steps:
        kpis.append(KpiCard(label="Agent turns", value=str(len(timeline_steps))))
        sections.append(
            ReportSection(
                heading="Agent timeline", html=_figure_html(timeline_figure(list(timeline_steps)))
            )
        )

    if insights:
        sections.append(
            ReportSection(
                heading="Agentic insights", html=f'<div class="insights">{insights}</div>'
            )
        )

    return render_report_html(
        title=title,
        subtitle=subtitle,
        sections=sections,
        kpis=kpis,
    )


def save_report(html: str, path: str) -> str:
    """Write the report to disk, creating parent directories as needed."""
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return str(target)
