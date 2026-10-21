"""Dark-mode Plotly dashboards and the standalone executive report.

Chart selection is data-driven rather than hard-coded: column kinds are
inferred from the scrubbed result batch (ISO timestamps, numerics, low-
cardinality categories), then timeseries, distributions, category bars and a
correlation heatmap are emitted only when the data can support them. Numbers
are computed in pure Python so the module does not require NumPy.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

import plotly.graph_objects as go

from datawhisper.core.agent import AgentStep
from datawhisper.core.models import PlanInfo, RedactionReport
from datawhisper.integrations.github_bot import (
    BenchmarkResult,
    DriftReport,
    PiiExposure,
    PlanRegression,
)

_BG = "#0d1117"
_CARD = "#161b22"
_BORDER = "#30363d"
_TEXT = "#c9d1d9"
_MUTED = "#8b949e"
_ACCENT = "#58a6ff"
_COLORS = ["#58a6ff", "#3fb950", "#d29922", "#f97583", "#bc8cff", "#39c5cf"]


class KpiCard:
    """Small value object so callers need no pydantic model for chrome."""

    __slots__ = ("detail", "label", "value")

    def __init__(self, label: str, value: str, detail: str = "") -> None:
        self.label = label
        self.value = value
        self.detail = detail


class ChartBlock:
    __slots__ = ("heading", "html")

    def __init__(self, heading: str, html: str) -> None:
        self.heading = heading
        self.html = html


def _dark_layout(title: str) -> go.Layout:
    return go.Layout(
        template="plotly_dark",
        title={"text": title, "font": {"color": _TEXT}},
        paper_bgcolor=_BG,
        plot_bgcolor=_BG,
        font={"color": _TEXT},
        margin={"l": 50, "r": 24, "t": 48, "b": 44},
    )


def _figure_html(figure: go.Figure, height: int = 340) -> str:
    figure.update_layout(height=height)
    return str(figure.to_html(full_html=False, include_plotlyjs=False))


# ---------------------------------------------------------------------------
# Column inference
# ---------------------------------------------------------------------------
def _is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if len(candidate) < 10:
        return None
    try:
        return datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        if len(candidate) == 10:
            try:
                return datetime.strptime(candidate, "%Y-%m-%d")
            except ValueError:
                return None
        return None


def _column_kind(values: Sequence[Any]) -> str:
    sample = [v for v in values if v is not None]
    if not sample:
        return "empty"
    if all(_is_numeric(v) for v in sample):
        return "numeric"
    if all(_parse_datetime(v) is not None for v in sample[:50]):
        return "datetime"
    return "categorical"


def _numeric_pairs(
    columns: Sequence[str], rows: Sequence[Sequence[Any]]
) -> list[tuple[str, list[float]]]:
    series: list[tuple[str, list[float]]] = []
    for index, name in enumerate(columns):
        values = [row[index] for row in rows if index < len(row)]
        if _column_kind(values) == "numeric":
            numeric = [float(v) for v in values if _is_numeric(v)]
            if numeric:
                series.append((name, numeric))
    return series


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    size = min(len(xs), len(ys))
    if size < 3:
        return None
    mean_x = sum(xs[:size]) / size
    mean_y = sum(ys[:size]) / size
    covariance = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(size))
    variance_x = sum((xs[i] - mean_x) ** 2 for i in range(size))
    variance_y = sum((ys[i] - mean_y) ** 2 for i in range(size))
    denominator = math.sqrt(variance_x * variance_y)
    if denominator == 0:
        return None
    return covariance / denominator


# ---------------------------------------------------------------------------
# Auto charts
# ---------------------------------------------------------------------------
def auto_charts(
    columns: Sequence[str], rows: Sequence[Sequence[Any]], max_charts: int = 6
) -> list[ChartBlock]:
    """Infer and render the charts this batch can actually support."""
    if not rows:
        return []
    blocks: list[ChartBlock] = []
    kinds = {
        name: _column_kind([row[index] for row in rows if index < len(row)])
        for index, name in enumerate(columns)
    }

    datetime_columns = [name for name, kind in kinds.items() if kind == "datetime"]
    numeric_columns = [name for name, kind in kinds.items() if kind == "numeric"]
    categorical_columns = [
        name
        for name, kind in kinds.items()
        if kind == "categorical" and 1 < len({row[columns.index(name)] for row in rows}) <= 25
    ]

    if datetime_columns and numeric_columns:
        time_column = datetime_columns[0]
        measure = numeric_columns[0]
        time_index, measure_index = columns.index(time_column), columns.index(measure)
        points = [
            (_parse_datetime(row[time_index]), row[measure_index])
            for row in rows
            if _is_numeric(row[measure_index]) and _parse_datetime(row[time_index]) is not None
        ]
        if points:
            figure = go.Figure(
                go.Scatter(
                    x=[p[0] for p in points],
                    y=[p[1] for p in points],
                    mode="lines+markers",
                    line={"color": _ACCENT},
                )
            )
            figure.update_layout(_dark_layout(f"{measure} over {time_column}"))
            figure.update_xaxes(gridcolor=_BORDER)
            figure.update_yaxes(gridcolor=_BORDER)
            blocks.append(ChartBlock("Timeseries", _figure_html(figure)))

    for name in numeric_columns[:3]:
        index = columns.index(name)
        values = [float(row[index]) for row in rows if _is_numeric(row[index])]
        figure = go.Figure(go.Histogram(x=values, marker_color=_ACCENT, nbinsx=24))
        figure.update_layout(_dark_layout(f"Distribution of {name}"))
        figure.update_xaxes(gridcolor=_BORDER)
        blocks.append(ChartBlock(f"Distribution · {name}", _figure_html(figure, height=300)))

    if len(numeric_columns) >= 2:
        matrix: list[list[float | None]] = []
        for left in numeric_columns:
            left_values = [
                float(r[columns.index(left)]) for r in rows if _is_numeric(r[columns.index(left)])
            ]
            row_values: list[float | None] = []
            for right in numeric_columns:
                right_values = [
                    float(r[columns.index(right)])
                    for r in rows
                    if _is_numeric(r[columns.index(right)])
                ]
                row_values.append(_pearson(left_values, right_values))
            matrix.append(row_values)
        if any(value is not None for line in matrix for value in line):
            figure = go.Figure(
                go.Heatmap(
                    z=[[value if value is not None else 0.0 for value in line] for line in matrix],
                    x=numeric_columns,
                    y=numeric_columns,
                    colorscale="RdBu",
                    zmin=-1,
                    zmax=1,
                    text=[
                        [f"{value:.2f}" if value is not None else "n/a" for value in line]
                        for line in matrix
                    ],
                    texttemplate="%{text}",
                )
            )
            figure.update_layout(_dark_layout("Correlation heatmap"))
            blocks.append(ChartBlock("Correlations", _figure_html(figure, height=380)))

    for name in categorical_columns[:2]:
        index = columns.index(name)
        counts = Counter(str(row[index]) for row in rows if row[index] is not None)
        if not counts:
            continue
        top = counts.most_common(12)
        figure = go.Figure(
            go.Bar(x=[item[0] for item in top], y=[item[1] for item in top], marker_color=_ACCENT)
        )
        figure.update_layout(_dark_layout(f"Counts by {name}"))
        figure.update_xaxes(tickangle=-30, gridcolor=_BORDER)
        figure.update_yaxes(gridcolor=_BORDER)
        blocks.append(ChartBlock(f"Categories · {name}", _figure_html(figure, height=300)))

    return blocks[:max_charts]


def timeline_figure(steps: Sequence[AgentStep]) -> go.Figure:
    figure = go.Figure()
    labels = [f"#{step.index} {step.action}" for step in steps][::-1]
    durations = [step.elapsed_ms for step in steps][::-1]
    figure.add_trace(
        go.Bar(
            orientation="h",
            x=durations,
            y=labels,
            marker_color=[_COLORS[step.index % len(_COLORS)] for step in steps][::-1],
            hovertext=[step.observation[:120] for step in steps][::-1],
        )
    )
    figure.update_layout(_dark_layout("Agent execution timeline (ms per turn)"))
    figure.update_xaxes(gridcolor=_BORDER)
    return figure


def timeline_block(steps: Sequence[AgentStep]) -> ChartBlock:
    """Public helper so the CLI need not touch private rendering internals."""
    return ChartBlock("Agent timeline", _figure_html(timeline_figure(steps)))


def drift_figure(drift: DriftReport) -> go.Figure:
    figure = go.Figure()
    tables = [table.table for table in drift.tables]
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
    figure = go.Figure()
    labels = [bench.label for bench in benchmarks]
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
            marker_color=["#f97583" if b.regression else "#3fb950" for b in benchmarks],
        )
    )
    figure.update_layout(_dark_layout("Query benchmarks (ms, lower is better)"), barmode="group")
    figure.update_yaxes(title_text="ms", gridcolor=_BORDER)
    return figure


def plan_html(plan: PlanInfo | None) -> str:
    """Render the normalized plan as a compact HTML table."""
    if plan is None:
        return '<p class="muted">No plan captured for this report.</p>'
    if plan.error:
        return f'<p class="muted">Planner unavailable: {plan.error}</p>'
    rows = "".join(
        f"<tr><td>{node.node_type}</td><td>{node.table or '—'}</td>"
        f"<td>{'' if node.estimated_rows is None else int(node.estimated_rows)}</td>"
        f"<td class='muted'>{node.detail[:120]}</td></tr>"
        for node in plan.nodes[:40]
    )
    return (
        "<table><thead><tr><th>Operator</th><th>Table</th><th>Est. rows</th>"
        f"<th>Detail</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def redaction_badges(report: RedactionReport | None) -> str:
    if report is None or report.total == 0:
        return '<span class="badge ok">no PII detected</span>'
    badges = "".join(
        f'<span class="badge warn">{category}: {count}</span>'
        for category, count in sorted(report.by_category.items())
    )
    return badges + f'<span class="badge">{report.mode}</span>'


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
  .wrap {{ max-width: 1100px; margin: 0 auto; padding: 32px 20px 64px; }}
  h1 {{ color: {_ACCENT}; font-size: 26px; margin: 0 0 4px; }}
  h2 {{ font-size: 18px; border-bottom: 1px solid {_BORDER};
        padding-bottom: 8px; margin-top: 36px; }}
  .meta {{ color: {_MUTED}; font-size: 13px; margin-bottom: 24px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; }}
  .card {{ background: {_CARD}; border: 1px solid {_BORDER}; border-radius: 12px; padding: 18px; }}
  .label {{ color: {_MUTED}; font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; }}
  .value {{ font-size: 28px; font-weight: 700; color: {_ACCENT}; margin-top: 6px; }}
  .detail, .muted {{ color: {_MUTED}; font-size: 12px; }}
  .insights {{ background: {_CARD}; border: 1px solid {_BORDER}; border-radius: 12px;
               padding: 16px 20px; line-height: 1.65; white-space: pre-wrap; }}
  .badge {{ display: inline-block; border: 1px solid {_BORDER}; border-radius: 999px;
            padding: 3px 10px; margin: 2px 6px 2px 0; font-size: 12px; color: {_TEXT}; }}
  .badge.ok {{ border-color: #238636; color: #3fb950; }}
  .badge.warn {{ border-color: #9e6a03; color: #d29922; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid {_BORDER}; }}
  th {{ color: {_MUTED}; font-weight: 600; }}
  a {{ color: {_ACCENT}; }}
</style>
</head>
<body>
<div class="wrap">
"""


def render_report_html(
    title: str,
    subtitle: str = "",
    blocks: Sequence[ChartBlock] = (),
    kpis: Sequence[KpiCard] = (),
    raw_sections: Sequence[tuple[str, str]] = (),
) -> str:
    parts = [_HEAD, f"<h1>{title}</h1>"]
    if subtitle:
        parts.append(f'<div class="meta">{subtitle}</div>')
    if kpis:
        parts.append(_kpi_grid(kpis))
    for block in blocks:
        parts.append(f"<h2>{block.heading}</h2>{block.html}")
    for heading, html in raw_sections:
        parts.append(f"<h2>{heading}</h2>{html}")
    parts.append("</div></body></html>\n")
    return "".join(parts)


def render_audit_html(
    *,
    drift: DriftReport | None = None,
    pii: Sequence[PiiExposure] | None = None,
    benchmarks: Sequence[BenchmarkResult] | None = None,
    plan_regressions: Sequence[PlanRegression] | None = None,
    insights: str | None = None,
    timeline_steps: Sequence[AgentStep] = (),
    plan: PlanInfo | None = None,
    redactions: RedactionReport | None = None,
    title: str = "DataWhisper Audit",
    subtitle: str = "",
) -> str:
    kpis: list[KpiCard] = []
    blocks: list[ChartBlock] = []
    pii_findings = list(pii or [])
    regressions = list(plan_regressions or [])
    bench_list = list(benchmarks or [])

    if drift is not None:
        kpis.append(KpiCard("drifted tables", str(len(drift.tables))))
        kpis.append(KpiCard("column changes", str(drift.column_changes)))
        if drift.has_changes:
            blocks.append(ChartBlock("Schema drift", _figure_html(drift_figure(drift))))
    if pii_findings:
        kpis.append(KpiCard("PII columns", str(len(pii_findings)), "introduced by this change"))
    if bench_list:
        regressions_count = sum(1 for b in bench_list if b.regression)
        kpis.append(KpiCard("latency regressions", str(regressions_count)))
        blocks.append(ChartBlock("Query benchmarks", _figure_html(benchmark_figure(bench_list))))
    if regressions:
        kpis.append(KpiCard("plan regressions", str(len(regressions))))
    if timeline_steps:
        kpis.append(KpiCard("agent turns", str(len(timeline_steps))))
        blocks.append(
            ChartBlock("Agent timeline", _figure_html(timeline_figure(list(timeline_steps))))
        )

    raw_sections: list[tuple[str, str]] = []
    raw_sections.append(("Redaction audit", f"<p>{redaction_badges(redactions)}</p>"))
    if plan is not None:
        raw_sections.append(("Query execution plan", plan_html(plan)))
    if regressions:
        rows = "".join(
            f"<tr><td><code>{r.label}</code></td><td>{r.kind}</td><td>{r.detail}</td></tr>"
            for r in regressions
        )
        raw_sections.append(
            (
                "Plan regressions",
                "<table><thead><tr><th>Query</th><th>Signal</th><th>Detail</th></tr>"
                f"</thead><tbody>{rows}</tbody></table>",
            )
        )
    if pii_findings:
        rows = "".join(
            f"<tr><td><code>{f.table}</code></td><td><code>{f.column}</code></td>"
            f"<td>{f.category.value}</td><td>{f.reason}</td></tr>"
            for f in pii_findings
        )
        raw_sections.append(
            (
                "PII exposure",
                "<table><thead><tr><th>Table</th><th>Column</th><th>Category</th>"
                f"<th>Origin</th></tr></thead><tbody>{rows}</tbody></table>",
            )
        )
    if insights:
        raw_sections.append(("Agentic insights", f'<div class="insights">{insights}</div>'))

    return render_report_html(
        title=title,
        subtitle=subtitle,
        blocks=blocks,
        kpis=kpis,
        raw_sections=raw_sections,
    )


def save_report(html: str, path: str) -> str:
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return str(target)


__all__ = [
    "ChartBlock",
    "KpiCard",
    "auto_charts",
    "benchmark_figure",
    "drift_figure",
    "plan_html",
    "redaction_badges",
    "render_audit_html",
    "render_report_html",
    "save_report",
    "timeline_block",
    "timeline_figure",
]
