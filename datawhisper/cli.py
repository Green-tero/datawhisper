"""DataWhisper command-line interface.

Two operating modes share one binary:

* **Interactive** - ``ask`` and ``dashboard`` run locally with rich spinners,
  colored tables and audit logs of every agent turn.
* **Headless CI** - ``audit`` is the entry point invoked by the GitHub Action.
  It never requires a TTY, degrades gracefully when no LLM key is present
  (deterministic drift + benchmarks only) and communicates verdicts through
  exit codes so workflows can gate merges on regressions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table as RichTable

from datawhisper.core.agent import AgentResult, DataWhisperAgent, ProviderError, build_provider
from datawhisper.core.config import DataWhisperConfig
from datawhisper.core.database import ExecutionEngine
from datawhisper.integrations.github_bot import (
    BenchmarkResult,
    DriftReport,
    GitHubAPIError,
    GitHubClient,
    compute_drift,
    format_audit_report,
    merge_drifts,
    parse_benchmark_file,
    run_benchmarks,
)
from datawhisper.visualization.dashboard import (
    KpiCard,
    ReportSection,
    _figure_html,
    render_audit_html,
    render_report_html,
    save_report,
    table_rowcount_figure,
    timeline_figure,
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="DataWhisper — local-first agentic tabular data engine & CI auditor.",
)
console = Console()

DEFAULT_AUDIT_QUESTION = (
    "Analyze the attached dataset: summarize volume, data-quality risks "
    "(nulls, skew, anomalies) and any pattern a reviewer should verify."
)


def _open_engine(source: str, config: DataWhisperConfig) -> ExecutionEngine:
    try:
        return ExecutionEngine.from_source(source, config.safety)
    except FileNotFoundError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def _print_agent_result(result: AgentResult) -> None:
    if result.steps:
        steps_table = RichTable(title="Agent trace", show_lines=False)
        steps_table.add_column("#", justify="right", style="dim")
        steps_table.add_column("action", style="cyan")
        steps_table.add_column("ms", justify="right")
        steps_table.add_column("observation", overflow="fold")
        for step in result.steps:
            observation = (
                step.observation if len(step.observation) <= 110 else step.observation[:107] + "..."
            )
            steps_table.add_row(str(step.index), step.action, f"{step.elapsed_ms:.1f}", observation)
        console.print(steps_table)
    style = "green" if result.success else "red"
    console.print(
        Panel(
            Markdown(result.answer),
            title=f"answer ({result.iterations} turns, {result.elapsed_ms:.0f} ms)",
            border_style=style,
        )
    )
    if result.sql:
        console.print(Panel(result.sql, title="final SQL", border_style="blue"))


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Natural-language analytics question.")],
    db: Annotated[str, typer.Option("--db", help="SQLite/DuckDB/CSV file path.")],
    timeout: Annotated[float | None, typer.Option(help="Per-query timeout (s).")] = None,
) -> None:
    """Run the self-correcting agent against a local dataset."""
    config = DataWhisperConfig.from_env()
    if config.llm.provider == "none":
        console.print(
            "[red]error:[/red] no LLM provider configured. Set GEMINI_API_KEY or "
            "OPENAI_API_KEY (or DW_LLM_PROVIDER/DW_LLM_MODEL) and retry."
        )
        raise typer.Exit(code=2)
    engine = _open_engine(db, config)
    try:
        try:
            provider = build_provider(config.llm)
        except ProviderError as exc:
            console.print(f"[red]provider error:[/red] {exc}")
            raise typer.Exit(code=2) from exc
        agent = DataWhisperAgent(engine, provider, config)
        with console.status("[bold cyan]datawhisper agent thinking...", spinner="dots"):
            result = agent.run(question)
        _print_agent_result(result)
        raise typer.Exit(code=0 if result.success else 1)
    finally:
        engine.close()


@app.command()
def inspect(
    db: Annotated[str, typer.Option("--db", help="SQLite/DuckDB/CSV file path.")],
    profile: Annotated[str | None, typer.Option(help="Profile 'table.column'.")] = None,
) -> None:
    """Print the catalog: tables, columns, types and row counts."""
    config = DataWhisperConfig.from_env()
    engine = _open_engine(db, config)
    try:
        snapshot = engine.introspect()
        catalog_table = RichTable(title=f"Catalog ({snapshot.dialect})")
        catalog_table.add_column("table", style="cyan")
        catalog_table.add_column("columns", overflow="fold")
        catalog_table.add_column("rows", justify="right")
        for table in snapshot.tables:
            columns = ", ".join(f"{c.name}:{c.type}" for c in table.columns)
            catalog_table.add_row(table.name, columns, str(table.row_count))
        console.print(catalog_table)

        if profile:
            try:
                table_name, column_name = profile.split(".", 1)
            except ValueError:
                console.print("[red]error:[/red] --profile expects 'table.column'")
                raise typer.Exit(code=2) from None
            prof = engine.profile_column(table_name, column_name)
            prof_table = RichTable(title=f"Profile {prof.table}.{prof.column}")
            prof_table.add_row("rows", str(prof.total_rows))
            prof_table.add_row("nulls", f"{prof.null_count} ({prof.null_ratio:.1%})")
            prof_table.add_row("distinct", str(prof.distinct_count))
            if prof.numeric_stats:
                prof_table.add_row(
                    "numeric",
                    f"min={prof.numeric_stats.minimum} max={prof.numeric_stats.maximum} "
                    f"mean={prof.numeric_stats.mean}",
                )
            for vc in prof.top_values:
                prof_table.add_row(f"value {vc.value}", str(vc.count))
            console.print(prof_table)
    finally:
        engine.close()


@app.command()
def dashboard(
    db: Annotated[str, typer.Option("--db", help="SQLite/DuckDB/CSV file path.")],
    output: Annotated[Path, typer.Option("--output", "-o", help="HTML output path.")] = Path(
        "datawhisper_dashboard.html"
    ),
    question: Annotated[str | None, typer.Option(help="Run the agent and embed its trace.")] = None,
) -> None:
    """Generate a standalone dark-mode Plotly dashboard for a dataset."""
    config = DataWhisperConfig.from_env()
    engine = _open_engine(db, config)
    try:
        snapshot = engine.introspect()
        total_rows = sum(t.row_count or 0 for t in snapshot.tables)
        total_columns = sum(len(t.columns) for t in snapshot.tables)
        kpis = [
            KpiCard(label="tables", value=str(len(snapshot.tables))),
            KpiCard(label="columns", value=str(total_columns)),
            KpiCard(label="rows", value=str(total_rows)),
            KpiCard(label="dialect", value=snapshot.dialect),
        ]
        sections = [
            ReportSection(
                heading="Data inventory", html=_figure_html(table_rowcount_figure(snapshot))
            )
        ]

        if question:
            if config.llm.provider == "none":
                console.print(
                    "[yellow]skip:[/yellow] --question needs an LLM key; "
                    "dashboard generated without agent trace."
                )
            else:
                try:
                    provider = build_provider(config.llm)
                except ProviderError as exc:
                    console.print(f"[yellow]skip:[/yellow] {exc}")
                else:
                    agent = DataWhisperAgent(engine, provider, config)
                    with console.status("[bold cyan]agent analyzing...", spinner="dots"):
                        result = agent.run(question)
                    sections.append(
                        ReportSection(
                            heading="Agent timeline",
                            html=_figure_html(timeline_figure(result.steps)),
                        )
                    )
                    kpis.append(KpiCard(label="agent turns", value=str(len(result.steps))))
                    _print_agent_result(result)

        html = render_report_html(
            title="DataWhisper Dashboard",
            subtitle=f"generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} • {db}",
            sections=sections,
            kpis=kpis,
        )
        path = save_report(html, str(output))
        console.print(f"[green]dashboard written:[/green] {path}")
    finally:
        engine.close()


@app.command()
def audit(
    repo: Annotated[str | None, typer.Option(help="owner/name; enables GitHub mode.")] = None,
    pr: Annotated[int | None, typer.Option(help="Pull request number.")] = None,
    base_ref: Annotated[str | None, typer.Option(help="Base git ref (GitHub mode).")] = None,
    head_ref: Annotated[str | None, typer.Option(help="Head git ref (GitHub mode).")] = None,
    base_sql: Annotated[Path | None, typer.Option(help="Local baseline DDL file.")] = None,
    head_sql: Annotated[Path | None, typer.Option(help="Local candidate DDL file.")] = None,
    baseline_db: Annotated[Path | None, typer.Option(help="Baseline DB for benchmarks.")] = None,
    candidate_db: Annotated[Path | None, typer.Option(help="Candidate DB for benchmarks.")] = None,
    bench_file: Annotated[Path | None, typer.Option(help="Benchmark suite file.")] = None,
    db: Annotated[Path | None, typer.Option(help="Dataset for agentic insights.")] = None,
    question: Annotated[
        str, typer.Option(help="Insight question for the agent.")
    ] = DEFAULT_AUDIT_QUESTION,
    output_dir: Annotated[Path, typer.Option(help="Report output directory.")] = Path(
        "datawhisper-report"
    ),
    artifact_url: Annotated[
        str | None, typer.Option(help="Link to the uploaded interactive report.")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print report; skip posting.")] = False,
    fail_on_regression: Annotated[
        bool, typer.Option("--fail-on-regression", help="Exit 1 on regressions.")
    ] = False,
) -> None:
    """Headless CI audit: schema drift, benchmarks, insights, sticky PR comment."""
    config = DataWhisperConfig.from_env()
    token = config.github.token
    started = datetime.now(UTC)

    client: GitHubClient | None = None
    drift_reports: list[DriftReport] = []
    benchmarks: list[BenchmarkResult] = []
    insights: str | None = None
    timeline_steps = []

    github_mode = bool(repo and pr and base_ref)
    local_mode = bool(base_sql and head_sql)
    if not github_mode and not local_mode:
        console.print(
            "[red]error:[/red] provide --repo/--pr/--base-ref (GitHub mode) or "
            "--base-sql/--head-sql (local mode)."
        )
        raise typer.Exit(code=2)

    try:
        if github_mode:
            if not token:
                console.print("[red]error:[/red] GitHub mode needs DATAWHISPER_GITHUB_TOKEN.")
                raise typer.Exit(code=2)
            client = GitHubClient(
                token=token,
                repo=repo or "",
                api_base=config.github.api_base,
                request_timeout=config.github.request_timeout,
            )
            with console.status("[cyan]fetching PR files...", spinner="dots"):
                changed = [
                    f.filename for f in client.list_pr_files(pr or 0) if f.filename.endswith(".sql")
                ]
                head_sha = head_ref or "HEAD"
                for filename in changed:
                    old = client.get_file_content(filename, base_ref or "")
                    new = client.get_file_content(filename, head_sha)
                    drift_reports.append(compute_drift(old, new, source=filename))
        else:
            old = (base_sql or Path()).read_text(encoding="utf-8")
            new = (head_sql or Path()).read_text(encoding="utf-8")
            drift_reports.append(compute_drift(old, new, source=(head_sql or Path()).name))

        drift = merge_drifts(drift_reports)

        if baseline_db and candidate_db and bench_file:
            queries = parse_benchmark_file(bench_file.read_text(encoding="utf-8"))
            baseline_engine = _open_engine(str(baseline_db), config)
            candidate_engine = _open_engine(str(candidate_db), config)
            try:
                with console.status("[cyan]benchmarking queries...", spinner="dots"):
                    benchmarks = run_benchmarks(baseline_engine, candidate_engine, queries)
            finally:
                baseline_engine.close()
                candidate_engine.close()

        if db is not None and config.llm.provider != "none":
            engine = _open_engine(str(db), config)
            try:
                provider = build_provider(config.llm)
                agent = DataWhisperAgent(engine, provider, config)
                with console.status("[cyan]agent auditing dataset...", spinner="dots"):
                    result = agent.run(question)
                insights = result.answer if result.success else None
                timeline_steps = result.steps
            except ProviderError as exc:
                console.print(f"[yellow]insights skipped:[/yellow] {exc}")
            finally:
                engine.close()

        markdown = format_audit_report(
            drift=drift,
            benchmarks=benchmarks or None,
            insights=insights,
            pr_number=pr,
            base_ref=base_ref or "",
            head_ref=head_ref or "",
            artifact_url=artifact_url,
            generated_at=started,
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "audit-report.md").write_text(markdown, encoding="utf-8")
        html = render_audit_html(
            drift=drift,
            benchmarks=benchmarks or None,
            insights=insights,
            timeline_steps=timeline_steps,
            title="DataWhisper Audit",
            subtitle=(
                f"{repo or 'local audit'} • generated " f"{started.strftime('%Y-%m-%d %H:%M UTC')}"
            ),
        )
        save_report(html, str(output_dir / "audit-report.html"))

        console.print(Markdown(markdown))

        if client is not None and not dry_run:
            with console.status("[cyan]posting PR comment...", spinner="dots"):
                outcome = client.upsert_sticky_comment(pr or 0, markdown)
            console.print(f"[green]PR comment {outcome}.[/green]")

        if fail_on_regression and any(b.regression for b in benchmarks):
            console.print("[red]verdict:[/red] performance regression detected.")
            raise typer.Exit(code=1)
        console.print("[green]verdict:[/green] audit complete.")
    except GitHubAPIError as exc:
        console.print(f"[red]GitHub API error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
