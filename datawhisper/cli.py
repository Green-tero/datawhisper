"""Typer/Rich CLI for local analysis and headless CI governance.

Two personas share one binary. Interactive commands (``ask``, ``inspect``,
``dashboard``) favour readable terminal output; ``audit --ci-audit`` is
non-interactive, needs no TTY and communicates through exit codes so a workflow
can gate merges. ``--explain-only`` paths never call a model and never execute
a query, which makes them safe on production replicas.
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

from datawhisper import __version__
from datawhisper.core.agent import AgentResult, DataWhisperAgent, ProviderError, build_provider
from datawhisper.core.config import DataWhisperConfig
from datawhisper.core.database import (
    BackendUnavailableError,
    ExecutionEngine,
    QueryExecutionError,
    QueryTimeoutError,
)
from datawhisper.core.models import PlanInfo
from datawhisper.core.privacy import PrivacyScrubber
from datawhisper.core.schema_graph import SchemaGraph
from datawhisper.core.security import CostDecision, SQLSecurityError
from datawhisper.integrations.github_bot import (
    BenchmarkResult,
    DriftReport,
    GitHubAPIError,
    GitHubClient,
    PlanRegression,
    analyze_pii_exposure,
    compute_drift,
    format_audit_report,
    merge_drifts,
    parse_benchmark_file,
    run_benchmarks,
)
from datawhisper.integrations.migration_audit import (
    MigrationReport,
    analyze_migration_sql,
)
from datawhisper.visualization.dashboard import (
    KpiCard,
    auto_charts,
    plan_html,
    redaction_badges,
    render_audit_html,
    render_report_html,
    save_report,
    timeline_block,
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="DataWhisper — zero-trust agentic data analysis & PR governance.",
)
console = Console()

DEFAULT_AUDIT_QUESTION = (
    "Summarize this dataset for a reviewer: volume, data-quality risks "
    "(nulls, skew, anomalies) and anything a human should verify."
)


def _quote_identifier(name: str) -> str:
    """Quote a catalog name for a locally constructed query."""
    return '"' + name.replace('"', '""') + '"'


def _open_engine(source: str, config: DataWhisperConfig) -> ExecutionEngine:
    try:
        return ExecutionEngine.from_source(source, config)
    except (FileNotFoundError, BackendUnavailableError) as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def _print_agent_result(result: AgentResult) -> None:
    if result.pruned_tables:
        console.print(
            f"[dim]context: {len(result.pruned_tables)} tables · "
            f"{result.join_hops} join hop(s)[/dim]"
        )
    if result.steps:
        trace = RichTable(title="Agent trace")
        trace.add_column("#", justify="right", style="dim")
        trace.add_column("action", style="cyan")
        trace.add_column("ms", justify="right")
        trace.add_column("observation", overflow="fold")
        for step in result.steps:
            observation = (
                step.observation if len(step.observation) <= 100 else step.observation[:97] + "..."
            )
            style = "red" if observation.startswith("[") else ""
            trace.add_row(
                str(step.index), step.action, f"{step.elapsed_ms:.1f}", observation, style=style
            )
        console.print(trace)
    if result.redactions is not None and result.redactions.total:
        console.print(
            f"[yellow]PII scrubbed:[/yellow] {result.redactions.total} value(s) "
            f"across {', '.join(result.redactions.columns)}"
        )
    console.print(
        Panel(
            Markdown(result.answer),
            title=f"answer · {result.iterations} turns · {result.elapsed_ms:.0f} ms",
            border_style="green" if result.success else "red",
        )
    )
    if result.sql:
        scrubber = PrivacyScrubber(DataWhisperConfig.from_env().privacy)
        safe_sql = scrubber.scrub_text(result.sql)[0]
        console.print(Panel(safe_sql, title="final SQL", border_style="blue"))


def _print_plan(plan: PlanInfo, decision: CostDecision) -> None:
    if plan.error:
        console.print(f"[yellow]planner unavailable:[/yellow] {plan.error}")
    else:
        table = RichTable(title=f"Query plan ({plan.dialect})")
        table.add_column("operator", style="cyan")
        table.add_column("table")
        table.add_column("est. rows", justify="right")
        table.add_column("detail", overflow="fold", style="dim")
        for node in plan.nodes[:30]:
            table.add_row(
                node.node_type,
                node.table or "—",
                "" if node.estimated_rows is None else f"{node.estimated_rows:.0f}",
                node.detail[:90],
            )
        console.print(table)
    verdict_style = "green" if decision.allowed else "red"
    body = "\n".join(f"• {reason}" for reason in decision.reasons) or "no findings"
    if decision.rewritten_sql:
        body += f"\n\nsuggested rewrite:\n{decision.rewritten_sql}"
    console.print(
        Panel(
            body,
            title=f"cost gate: {decision.action} "
            f"(~{decision.estimated_scan_rows:.0f} scanned rows)",
            border_style=verdict_style,
        )
    )


@app.command()
def version() -> None:
    """Print the installed DataWhisper version."""
    console.print(f"datawhisper {__version__}")


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Natural-language analytics question.")],
    db: Annotated[
        str, typer.Option("--db", help="SQLite/DuckDB/Parquet/JSONL/CSV path or Postgres DSN.")
    ],
    explain_only: Annotated[
        bool,
        typer.Option(
            "--explain-only", help="Show the pruned context; do not call the model or run SQL."
        ),
    ] = False,
) -> None:
    """Run the self-correcting agent against a dataset."""
    config = DataWhisperConfig.from_env()
    engine = _open_engine(db, config)
    try:
        catalog = engine.introspect()
        pruned = SchemaGraph(catalog, config.schema_graph).retrieve(
            question, config.schema_graph.max_tables
        )
        if explain_only:
            console.print(
                Panel(
                    pruned.render(config.schema_graph.max_context_tokens),
                    title="pruned schema context",
                    border_style="blue",
                )
            )
            console.print(
                f"[dim]{len(pruned.tables)} of {len(catalog.tables)} tables selected · "
                f"{pruned.join_plan.hops if pruned.join_plan else 0} join hop(s) · "
                "no model call, no query executed[/dim]"
            )
            return

        if config.llm.provider == "none":
            console.print(
                "[red]error:[/red] no LLM provider configured. Set GEMINI_API_KEY or "
                "OPENAI_API_KEY, or use --explain-only."
            )
            raise typer.Exit(code=2)
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
def explain(
    sql: Annotated[str, typer.Argument(help="Read-only SQL statement to explain.")],
    db: Annotated[str, typer.Option("--db", help="Data source path or DSN.")],
) -> None:
    """Dry-run a statement through the sandbox and cost gate (no execution)."""
    config = DataWhisperConfig.from_env()
    engine = _open_engine(db, config)
    try:
        try:
            plan, decision = engine.explain(sql)
        except SQLSecurityError as exc:
            console.print(f"[red]sandbox rejected the statement:[/red] {exc}")
            raise typer.Exit(code=2) from exc
        _print_plan(plan, decision)
        raise typer.Exit(code=0 if decision.allowed else 1)
    finally:
        engine.close()


@app.command()
def inspect(
    db: Annotated[str, typer.Option("--db", help="Data source path or DSN.")],
    profile: Annotated[str | None, typer.Option(help="Profile 'table.column'.")] = None,
    graph: Annotated[
        str | None, typer.Option(help="Show the pruned schema for this question.")
    ] = None,
) -> None:
    """Print the catalog, profile a column, or preview schema pruning."""
    config = DataWhisperConfig.from_env()
    engine = _open_engine(db, config)
    try:
        catalog = engine.introspect()
        if graph:
            pruned = SchemaGraph(catalog, config.schema_graph).retrieve(
                graph, config.schema_graph.max_tables
            )
            console.print(
                Panel(
                    pruned.render(config.schema_graph.max_context_tokens),
                    title=f"pruned context for: {graph}",
                    border_style="blue",
                )
            )
            console.print(
                f"[dim]{len(pruned.tables)} of {len(catalog.tables)} tables · "
                f"{len(pruned.dropped_tables)} pruned away[/dim]"
            )
            return

        table = RichTable(title=f"Catalog ({catalog.dialect})")
        table.add_column("table", style="cyan")
        table.add_column("columns", overflow="fold")
        table.add_column("rows", justify="right")
        for entry in catalog.tables:
            columns = ", ".join(f"{c.name}:{c.type}" for c in entry.columns[:8])
            if len(entry.columns) > 8:
                columns += f", +{len(entry.columns) - 8} more"
            table.add_row(entry.name, columns, str(entry.row_count))
        console.print(table)

        if profile:
            try:
                table_name, column_name = profile.split(".", 1)
            except ValueError:
                console.print("[red]error:[/red] --profile expects 'table.column'")
                raise typer.Exit(code=2) from None
            result = engine.profile_column(table_name, column_name)
            details = RichTable(title=f"Profile {result.table}.{result.column}")
            details.add_row("rows", str(result.total_rows))
            details.add_row("nulls", f"{result.null_count} ({result.null_ratio:.1%})")
            details.add_row("distinct", str(result.distinct_count))
            if result.numeric_stats:
                details.add_row(
                    "numeric",
                    f"min={result.numeric_stats.minimum} max={result.numeric_stats.maximum} "
                    f"mean={result.numeric_stats.mean}",
                )
            for value in result.top_values:
                details.add_row(f"value {value.value}", str(value.count))
            console.print(details)
            if result.redactions and result.redactions.total:
                console.print(
                    f"[yellow]PII scrubbed in profile:[/yellow] "
                    f"{result.redactions.total} value(s)"
                )
    finally:
        engine.close()


@app.command()
def dashboard(
    db: Annotated[str, typer.Option("--db", help="Data source path or DSN.")],
    output: Annotated[Path, typer.Option("--output", "-o", help="HTML output path.")] = Path(
        "datawhisper_dashboard.html"
    ),
    sql: Annotated[
        str | None,
        typer.Option(help="Query to visualize (defaults to a sample of the first table)."),
    ] = None,
    question: Annotated[str | None, typer.Option(help="Run the agent and embed its trace.")] = None,
) -> None:
    """Generate a standalone dark-mode dashboard with auto-detected charts."""
    config = DataWhisperConfig.from_env()
    engine = _open_engine(db, config)
    try:
        catalog = engine.introspect()
        total_rows = sum(t.row_count or 0 for t in catalog.tables)
        total_columns = sum(len(t.columns) for t in catalog.tables)

        sample_sql = sql
        if sample_sql is None and catalog.tables:
            sample_sql = f"SELECT * FROM {_quote_identifier(catalog.tables[0].name)} LIMIT 200"

        kpis = [
            KpiCard("tables", str(len(catalog.tables))),
            KpiCard("columns", str(total_columns)),
            KpiCard("rows", str(total_rows)),
            KpiCard("dialect", catalog.dialect),
        ]
        blocks = []
        plan: PlanInfo | None = None
        redactions = None
        if sample_sql:
            try:
                result = engine.execute(sample_sql, max_rows=200)
            except (SQLSecurityError, QueryExecutionError, QueryTimeoutError) as exc:
                console.print(f"[yellow]sample query skipped:[/yellow] {exc}")
            else:
                plan = result.plan
                redactions = result.redactions
                blocks.extend(auto_charts(result.columns, result.rows))

        if question:
            if config.llm.provider == "none":
                console.print("[yellow]skip:[/yellow] --question needs an LLM key.")
            else:
                try:
                    provider = build_provider(config.llm)
                except ProviderError as exc:
                    console.print(f"[yellow]skip:[/yellow] {exc}")
                else:
                    agent = DataWhisperAgent(engine, provider, config)
                    with console.status("[bold cyan]agent analyzing...", spinner="dots"):
                        agent_result = agent.run(question)
                    redactions = agent_result.redactions or redactions
                    kpis.append(KpiCard("agent turns", str(len(agent_result.steps))))
                    _print_agent_result(agent_result)
                    blocks.append(timeline_block(agent_result.steps))

        redaction_groups = len(redactions.by_category) if redactions else 0
        html = render_report_html(
            title="DataWhisper Dashboard",
            subtitle=(
                f"generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} • {db} • "
                f"PII: {redaction_groups} redaction group(s)"
            ),
            blocks=blocks,
            kpis=kpis,
            raw_sections=[
                ("Redaction audit", f"<p>{redaction_badges(redactions)}</p>"),
                ("Query execution plan", plan_html(plan)),
            ],
        )
        path = save_report(html, str(output))
        console.print(f"[green]dashboard written:[/green] {path}")
    finally:
        engine.close()


@app.command()
def audit(
    ci_audit: Annotated[
        bool, typer.Option("--ci-audit", help="Headless CI mode: no prompts, exit codes only.")
    ] = False,
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
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the report; do not post.")
    ] = False,
    fail_on_regression: Annotated[
        bool, typer.Option("--fail-on-regression", help="Exit 1 on latency/plan regressions.")
    ] = False,
    fail_on_pii: Annotated[
        bool, typer.Option("--fail-on-pii", help="Exit 1 when new sensitive columns appear.")
    ] = False,
) -> None:
    """Headless governance audit: drift, PII exposure, plans, benchmarks, insights."""
    config = DataWhisperConfig.from_env()
    started = datetime.now(UTC)
    token = config.github.token
    scrubber = PrivacyScrubber(config.privacy)

    github_mode = bool(repo and pr and base_ref)
    local_mode = bool(base_sql and head_sql)
    if not github_mode and not local_mode:
        console.print(
            "[red]error:[/red] provide --repo/--pr/--base-ref (GitHub mode) or "
            "--base-sql/--head-sql (local mode)."
        )
        raise typer.Exit(code=2)

    client: GitHubClient | None = None
    drift_reports: list[DriftReport] = []
    pii_findings = []
    migration_reports: list[MigrationReport] = []

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
                    item.filename
                    for item in client.list_pr_files(pr or 0)
                    if item.filename.endswith(".sql")
                ]
                head = head_ref or "HEAD"
                for filename in changed:
                    old = client.get_file_content(filename, base_ref or "")
                    new = client.get_file_content(filename, head)
                    drift_reports.append(compute_drift(old, new, source=filename))
                    pii_findings.extend(analyze_pii_exposure(old, new))
                    if "migration" in filename.lower():
                        migration_reports.append(analyze_migration_sql(new, source=filename))
        else:
            old_text = (base_sql or Path()).read_text(encoding="utf-8")
            new_text = (head_sql or Path()).read_text(encoding="utf-8")
            drift_reports.append(
                compute_drift(old_text, new_text, source=(head_sql or Path()).name)
            )
            pii_findings.extend(analyze_pii_exposure(old_text, new_text))
            migration_reports.append(
                analyze_migration_sql(new_text, source=(head_sql or Path()).name)
            )

        drift = merge_drifts(drift_reports)

        benchmarks: list[BenchmarkResult] = []
        plan_regressions: list[PlanRegression] = []
        if baseline_db and candidate_db and bench_file:
            queries = parse_benchmark_file(bench_file.read_text(encoding="utf-8"))
            baseline_engine = _open_engine(str(baseline_db), config)
            candidate_engine = _open_engine(str(candidate_db), config)
            try:
                with console.status("[cyan]benchmarking queries & plans...", spinner="dots"):
                    benchmarks, plan_regressions = run_benchmarks(
                        baseline_engine, candidate_engine, queries
                    )
            finally:
                baseline_engine.close()
                candidate_engine.close()

        insights: str | None = None
        timeline_steps = []
        if db is not None and config.llm.provider != "none":
            engine = _open_engine(str(db), config)
            try:
                provider = build_provider(config.llm)
                agent = DataWhisperAgent(engine, provider, config)
                with console.status("[cyan]agent auditing dataset...", spinner="dots"):
                    agent_result = agent.run(question)
                timeline_steps = agent_result.steps
                insights = agent_result.answer if agent_result.success else None
            except ProviderError as exc:
                console.print(f"[yellow]insights skipped:[/yellow] {exc}")
            finally:
                engine.close()

        markdown = format_audit_report(
            drift=drift,
            pii=pii_findings,
            benchmarks=benchmarks or None,
            plan_regressions=plan_regressions or None,
            migrations=migration_reports or None,
            insights=insights,
            pr_number=pr,
            base_ref=base_ref or "",
            head_ref=head_ref or "",
            artifact_url=artifact_url,
            generated_at=started,
            scrubber=scrubber,
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "audit-report.md").write_text(markdown, encoding="utf-8")
        html = render_audit_html(
            drift=drift,
            pii=pii_findings,
            benchmarks=benchmarks or None,
            plan_regressions=plan_regressions or None,
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

        latency_regressions = [bench for bench in benchmarks if bench.regression]
        if fail_on_pii and pii_findings:
            console.print(
                f"[red]verdict:[/red] {len(pii_findings)} sensitive column(s) introduced."
            )
            raise typer.Exit(code=1)
        if fail_on_regression and (latency_regressions or plan_regressions):
            console.print(
                f"[red]verdict:[/red] {len(latency_regressions)} latency and "
                f"{len(plan_regressions)} plan regression(s)."
            )
            raise typer.Exit(code=1)
        if ci_audit:
            console.print("[green]verdict:[/green] audit complete (CI mode).")
        else:
            console.print("[green]verdict:[/green] audit complete.")
    except GitHubAPIError as exc:
        console.print(f"[red]GitHub API error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
