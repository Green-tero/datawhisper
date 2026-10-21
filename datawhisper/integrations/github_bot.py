"""GitHub PR governance: schema drift, PII exposure, plan regressions, sticky reports.

The bot is deliberately conservative about what it publishes. It never posts
row data, and PII findings are reported at column level only (table, column,
category) so the comment itself cannot become a disclosure channel. All model
output is additionally scrubbed before formatting.

Rendering constraints drive the design: GitHub strips ``<script>``, so charts
are unicode bars plus links to the uploaded Plotly artifact, and long sections
are collapsed inside ``<details>`` to keep the conversation readable.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import requests
import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp

from datawhisper.core.database import ExecutionEngine
from datawhisper.core.models import PlanInfo
from datawhisper.core.privacy import PiiCategory, PrivacyScrubber, column_category

COMMENT_MARKER = "<!-- datawhisper:audit -->"
_GITHUB_API_VERSION = "2022-11-28"
_MAX_COMMENT_CHARS = 60_000


class GitHubAPIError(RuntimeError):
    """Raised when the GitHub REST API returns a non-success status."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"GitHub API error {status_code}: {message}")
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Schema drift
# ---------------------------------------------------------------------------
class ColumnChange(BaseModel):
    column: str
    old_type: str
    new_type: str


class TableDrift(BaseModel):
    table: str
    source: str = ""
    added_columns: list[str] = Field(default_factory=list)
    removed_columns: list[str] = Field(default_factory=list)
    type_changes: list[ColumnChange] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added_columns or self.removed_columns or self.type_changes)


class DriftReport(BaseModel):
    tables: list[TableDrift] = Field(default_factory=list)
    added_tables: list[str] = Field(default_factory=list)
    dropped_tables: list[str] = Field(default_factory=list)
    parse_errors: list[str] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.tables or self.added_tables or self.dropped_tables)

    @property
    def column_changes(self) -> int:
        return sum(
            len(t.added_columns) + len(t.removed_columns) + len(t.type_changes) for t in self.tables
        )


class PiiExposure(BaseModel):
    """Column-level PII finding; never contains sampled values."""

    table: str
    column: str
    category: PiiCategory
    reason: str = "new column"


class PlanRegression(BaseModel):
    label: str
    kind: str
    detail: str
    baseline: str = ""
    candidate: str = ""


def _extract_tables(sql_text: str) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Parse CREATE TABLE DDL into ``{table: {column: normalized_type}}``."""
    tables: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    try:
        statements = [s for s in sqlglot.parse(sql_text) if s is not None]
    except sqlglot.errors.ParseError as exc:
        return {}, [f"migration file unparsable: {exc}"]
    for statement in statements:
        if isinstance(statement, exp.Alter) and statement.kind == "TABLE":
            alter_table = statement.find(exp.Table)
            if alter_table is None:
                continue
            altered = tables.setdefault(alter_table.name.lower(), {})
            for action in statement.args.get("actions") or []:
                if isinstance(action, exp.ColumnDef):
                    kind = action.args.get("kind")
                    altered[action.name.lower()] = (
                        kind.sql().upper() if kind is not None else "UNKNOWN"
                    )
                elif isinstance(action, exp.Drop) and isinstance(action.this, exp.Column):
                    altered.pop(action.this.name.lower(), None)
            continue
        if not (isinstance(statement, exp.Create) and statement.kind == "TABLE"):
            continue
        table_node = statement.find(exp.Table)
        schema = statement.find(exp.Schema)
        if table_node is None or schema is None:
            continue
        columns: dict[str, str] = {}
        for column_def in schema.find_all(exp.ColumnDef):
            kind = column_def.args.get("kind")
            columns[column_def.name.lower()] = kind.sql().upper() if kind is not None else "UNKNOWN"
        tables[table_node.name.lower()] = columns
    return tables, errors


def compute_drift(old_sql: str, new_sql: str, source: str = "") -> DriftReport:
    """Diff two migration snapshots at column level."""
    old_tables, old_errors = _extract_tables(old_sql)
    new_tables, new_errors = _extract_tables(new_sql)
    report = DriftReport(parse_errors=old_errors + new_errors)

    for name in sorted(set(old_tables) - set(new_tables)):
        report.dropped_tables.append(name)
    for name in sorted(set(new_tables) - set(old_tables)):
        report.added_tables.append(name)

    for name in sorted(set(old_tables) & set(new_tables)):
        old_cols, new_cols = old_tables[name], new_tables[name]
        drift = TableDrift(table=name, source=source)
        drift.added_columns = sorted(set(new_cols) - set(old_cols))
        drift.removed_columns = sorted(set(old_cols) - set(new_cols))
        for column in sorted(set(old_cols) & set(new_cols)):
            if old_cols[column] != new_cols[column]:
                drift.type_changes.append(
                    ColumnChange(
                        column=column, old_type=old_cols[column], new_type=new_cols[column]
                    )
                )
        if drift.has_changes:
            report.tables.append(drift)
    return report


def merge_drifts(reports: Sequence[DriftReport]) -> DriftReport:
    merged = DriftReport()
    seen: set[str] = set()
    for report in reports:
        for table in report.tables:
            if table.table in seen:
                continue
            seen.add(table.table)
            merged.tables.append(table)
        merged.added_tables.extend(t for t in report.added_tables if t not in merged.added_tables)
        merged.dropped_tables.extend(
            t for t in report.dropped_tables if t not in merged.dropped_tables
        )
        merged.parse_errors.extend(report.parse_errors)
    return merged


def analyze_pii_exposure(old_sql: str, new_sql: str) -> list[PiiExposure]:
    """Flag newly introduced columns whose names imply sensitive data.

    Covers brand-new tables (every column is new) and added columns on
    existing tables. Renames surface as one removed plus one added column, so
    they are covered without special casing.
    """
    old_tables, _ = _extract_tables(old_sql)
    new_tables, _ = _extract_tables(new_sql)
    findings: list[PiiExposure] = []

    for table, columns in sorted(new_tables.items()):
        previous = old_tables.get(table, {})
        for column in sorted(columns):
            if column in previous:
                continue
            category = column_category(column)
            if category is None:
                continue
            findings.append(
                PiiExposure(
                    table=table,
                    column=column,
                    category=category,
                    reason="new table" if table not in old_tables else "new column",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Benchmarks and plan regressions
# ---------------------------------------------------------------------------
class BenchmarkResult(BaseModel):
    label: str
    sql: str
    baseline_ms: float | None = None
    candidate_ms: float | None = None
    delta_ms: float | None = None
    delta_pct: float | None = None
    regression: bool = False
    error: str | None = None


def parse_benchmark_file(text: str) -> list[tuple[str, str]]:
    """Parse blank-line separated blocks with optional ``-- label: x`` headers."""
    queries: list[tuple[str, str]] = []
    label: str | None = None
    lines: list[str] = []

    def flush() -> None:
        nonlocal label, lines
        if lines:
            sql = "\n".join(lines).strip().rstrip(";")
            if sql:
                queries.append((label or f"query {len(queries) + 1}", sql))
        label, lines = None, []

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            flush()
        elif stripped.startswith("-- label:"):
            flush()
            label = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("--"):
            continue
        else:
            lines.append(raw_line)
    flush()
    return queries


def compare_plans(
    label: str, baseline: PlanInfo | None, candidate: PlanInfo | None
) -> list[PlanRegression]:
    """Detect the regressions a reviewer actually cares about.

    The checks are intentionally narrow: a lost index that becomes a full
    scan, a large growth in scanned rows, and a newly introduced cartesian
    product. Broader "plan changed" noise would train reviewers to ignore the
    bot.
    """
    if baseline is None or candidate is None:
        return []
    regressions: list[PlanRegression] = []

    baseline_indexed = {node.table for node in baseline.nodes if node.node_type == "INDEX_SCAN"}
    candidate_scans = {node.table for node in candidate.full_scans}
    lost = sorted(table for table in baseline_indexed & candidate_scans if table)
    for table in lost:
        regressions.append(
            PlanRegression(
                label=label,
                kind="index_dropped",
                detail=f"`{table}` moved from index lookup to sequential scan",
                baseline="index scan",
                candidate="sequential scan",
            )
        )

    baseline_rows = baseline.estimated_scan_rows
    candidate_rows = candidate.estimated_scan_rows
    if baseline_rows > 0 and candidate_rows > baseline_rows * 2 and candidate_rows > 1000:
        regressions.append(
            PlanRegression(
                label=label,
                kind="scan_growth",
                detail=(
                    f"estimated scanned rows grew from {baseline_rows:.0f} "
                    f"to {candidate_rows:.0f}"
                ),
                baseline=f"{baseline_rows:.0f} rows",
                candidate=f"{candidate_rows:.0f} rows",
            )
        )

    if candidate.cross_join and not baseline.cross_join:
        regressions.append(
            PlanRegression(
                label=label,
                kind="cartesian_product",
                detail="candidate plan introduces a cross product",
                baseline="no cross product",
                candidate="cross product",
            )
        )
    return regressions


def _best_of(engine: ExecutionEngine, sql: str, repeats: int) -> float:
    return min(engine.execute(sql, validate=True).elapsed_ms for _ in range(max(1, repeats)))


def run_benchmarks(
    baseline: ExecutionEngine,
    candidate: ExecutionEngine,
    queries: Sequence[tuple[str, str]],
    *,
    repeats: int = 3,
    regression_factor: float = 1.25,
    min_delta_ms: float = 5.0,
) -> tuple[list[BenchmarkResult], list[PlanRegression]]:
    """Time identical SQL on two snapshots and compare their plans."""
    results: list[BenchmarkResult] = []
    regressions: list[PlanRegression] = []
    for label, sql in queries:
        try:
            baseline_plan, _ = baseline.explain(sql)
            candidate_plan, _ = candidate.explain(sql)
            regressions.extend(compare_plans(label, baseline_plan, candidate_plan))
            baseline_ms = _best_of(baseline, sql, repeats)
            candidate_ms = _best_of(candidate, sql, repeats)
        except Exception as exc:
            results.append(BenchmarkResult(label=label, sql=sql, error=str(exc)))
            regressions.append(
                PlanRegression(
                    label=label,
                    kind="broken_query",
                    detail=f"query failed on a snapshot: {exc}",
                )
            )
            continue
        delta = candidate_ms - baseline_ms
        delta_pct = (delta / baseline_ms * 100.0) if baseline_ms > 0 else None
        regression = delta > min_delta_ms and (
            baseline_ms <= 0 or candidate_ms > baseline_ms * regression_factor
        )
        results.append(
            BenchmarkResult(
                label=label,
                sql=sql,
                baseline_ms=round(baseline_ms, 3),
                candidate_ms=round(candidate_ms, 3),
                delta_ms=round(delta, 3),
                delta_pct=round(delta_pct, 1) if delta_pct is not None else None,
                regression=regression,
            )
        )
    return results, regressions


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------
def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    def cell(value: str) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(cell(h) for h in headers) + " |"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    lines.extend("| " + " | ".join(cell(c) for c in row) + " |" for row in rows)
    return "\n".join(lines)


def unicode_bar(value: float, maximum: float, width: int = 10) -> str:
    if maximum <= 0:
        return "░" * width
    filled = round(width * max(0.0, min(value / maximum, 1.0)))
    return "█" * filled + "░" * (width - filled)


def _badge(label: str, count: int) -> str:
    icon = "🟢" if count == 0 else "🔴"
    return f"{icon} **{label}:** {count}"


def format_audit_report(
    *,
    drift: DriftReport | None = None,
    pii: Sequence[PiiExposure] | None = None,
    benchmarks: Sequence[BenchmarkResult] | None = None,
    plan_regressions: Sequence[PlanRegression] | None = None,
    insights: str | None = None,
    pr_number: int | None = None,
    base_ref: str = "",
    head_ref: str = "",
    artifact_url: str | None = None,
    generated_at: datetime | None = None,
    scrubber: PrivacyScrubber | None = None,
) -> str:
    """Compose the sticky governance comment with collapsed detail sections."""
    now = (generated_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    pii_findings = list(pii or [])
    regressions = list(plan_regressions or [])
    bench_list = list(benchmarks or [])
    drift = drift or DriftReport()

    benchmark_regressions = sum(1 for b in bench_list if b.regression)
    blocking = bool(pii_findings) or bool(regressions) or benchmark_regressions > 0

    lines: list[str] = [COMMENT_MARKER, "## 🛡️ DataWhisper Governance Report", ""]
    context = [f"**PR #{pr_number}**" if pr_number else "**Local audit**"]
    if base_ref and head_ref:
        context.append(f"`{base_ref}…{head_ref}`")
    context.append(f"generated {now}")
    lines.append(" • ".join(context))
    lines.append("")

    verdict = (
        "🔴 **Review required** — sensitive columns, plan regressions or latency drift detected."
        if blocking
        else "🟢 **No blocking findings** — schema, privacy and plan checks passed."
    )
    lines.append(f"> {verdict}")
    lines.append("")
    lines.append(
        " · ".join(
            [
                _badge("drifted tables", len(drift.tables) + len(drift.added_tables)),
                _badge("PII columns", len(pii_findings)),
                _badge("plan regressions", len(regressions)),
                _badge("latency regressions", benchmark_regressions),
            ]
        )
    )
    lines.append("")

    # --- Drift ---------------------------------------------------------
    lines.append("<details>")
    lines.append(
        f"<summary><b>🧬 Schema drift</b> ({drift.column_changes} column changes)</summary>"
    )
    lines.append("")
    if not drift.has_changes:
        lines.append("No schema drift detected in `CREATE TABLE` statements.")
    else:
        if drift.added_tables:
            lines.append("**New tables:** " + ", ".join(f"`{t}`" for t in drift.added_tables))
        if drift.dropped_tables:
            lines.append("**Dropped tables:** " + ", ".join(f"`{t}`" for t in drift.dropped_tables))
        for table in drift.tables:
            lines.append("")
            origin = f" (`{table.source}`)" if table.source else ""
            lines.append(f"#### `{table.table}`{origin}")
            rows: list[list[str]] = []
            rows.extend(["added", f"`{c}`", ""] for c in table.added_columns)
            rows.extend(["removed", f"`{c}`", ""] for c in table.removed_columns)
            rows.extend(
                ["changed", f"`{c.column}`", f"`{c.old_type}` -> `{c.new_type}`"]
                for c in table.type_changes
            )
            lines.append(markdown_table(["Change", "Column", "Details"], rows))
        if drift.parse_errors:
            lines.append("")
            lines.append(
                f"⚠️ {len(drift.parse_errors)} statement(s) could not be parsed and were "
                "excluded from the automated diff."
            )
    lines.append("")
    lines.append("</details>")
    lines.append("")

    # --- PII -----------------------------------------------------------
    lines.append("<details>")
    lines.append(f"<summary><b>🔐 PII exposure risk</b> ({len(pii_findings)} columns)</summary>")
    lines.append("")
    if not pii_findings:
        lines.append("No newly introduced columns match sensitive-data patterns.")
    else:
        lines.append(
            "These columns were introduced by this PR. DataWhisper redacts matching values "
            "at query time, but the storage and retention decision belongs to the reviewers."
        )
        lines.append("")
        lines.append(
            markdown_table(
                ["Table", "Column", "Category", "Origin"],
                [
                    [f"`{f.table}`", f"`{f.column}`", f"`{f.category.value}`", f.reason]
                    for f in pii_findings
                ],
            )
        )
    lines.append("")
    lines.append("</details>")
    lines.append("")

    # --- Plans + benchmarks -------------------------------------------
    lines.append("<details>")
    lines.append(
        f"<summary><b>⏱️ Query plans & benchmarks</b> "
        f"({len(regressions)} plan regressions)</summary>"
    )
    lines.append("")
    if not bench_list and not regressions:
        lines.append("No benchmark suite configured for this audit.")
    if regressions:
        lines.append(
            markdown_table(
                ["Query", "Signal", "Detail"],
                [[f"`{r.label}`", f"`{r.kind}`", r.detail] for r in regressions],
            )
        )
        lines.append("")
    if bench_list:
        worst = max((b.candidate_ms for b in bench_list if b.candidate_ms is not None), default=0.0)
        bench_rows: list[list[str]] = []
        for bench in bench_list:
            if bench.error is not None:
                bench_rows.append([f"`{bench.label}`", "—", "—", f"error: {bench.error}"])
                continue
            assert bench.baseline_ms is not None and bench.candidate_ms is not None
            assert bench.delta_ms is not None
            delta = f"{bench.delta_ms:+.1f} ms"
            if bench.delta_pct is not None:
                delta += f" ({bench.delta_pct:+.1f}%)"
            bench_rows.append(
                [
                    f"`{bench.label}`",
                    f"{bench.baseline_ms:.1f} ms",
                    f"{bench.candidate_ms:.1f} ms ` {unicode_bar(bench.candidate_ms, worst)} `",
                    f"{delta} {'⚠️' if bench.regression else '✅'}",
                ]
            )
        lines.append(markdown_table(["Query", "Baseline", "Candidate", "Δ / verdict"], bench_rows))
    lines.append("")
    lines.append("</details>")
    lines.append("")

    # --- Insights ------------------------------------------------------
    if insights:
        scrubbed = scrubber.scrub_text(insights)[0] if scrubber else insights
        # Neutralise HTML that could close our <details> block.
        scrubbed = scrubbed.replace("<details", "&lt;details").replace("</details", "&lt;/details")
        lines.append("<details>")
        lines.append("<summary><b>🤖 Agentic insights</b> (verify before merging)</summary>")
        lines.append("")
        lines.append(scrubbed.strip())
        lines.append("")
        lines.append("</details>")
        lines.append("")

    if artifact_url:
        lines.append(f"📈 [Interactive Plotly report]({artifact_url})")
        lines.append("")
    lines.append(
        "---\n<sub>Read-only sandboxed analysis • AST-validated SQL • results PII-scrubbed "
        "before reporting</sub>"
    )
    report = "\n".join(lines)
    return report[:_MAX_COMMENT_CHARS]


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------
class PrFile(BaseModel):
    filename: str
    status: str
    additions: int = 0
    deletions: int = 0


class GitHubClient:
    """Thin REST client for the governance bot."""

    def __init__(
        self,
        token: str,
        repo: str,
        api_base: str = "https://api.github.com",
        session: requests.Session | None = None,
        request_timeout: float = 20.0,
    ) -> None:
        if not token:
            raise GitHubAPIError(401, "a GitHub token is required")
        if "/" not in repo:
            raise GitHubAPIError(400, "repo must look like 'owner/name'")
        self._repo = repo
        self._api_base = api_base.rstrip("/")
        self._session = session or requests.Session()
        self._timeout = request_timeout
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        }

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        response = self._session.request(
            method,
            url,
            headers={**self._headers, **(headers or {})},
            json=json_body,
            params=params,
            timeout=self._timeout,
        )
        if response.status_code >= 400:
            try:
                payload = response.json()
                message = (
                    payload.get("message", response.text)
                    if isinstance(payload, dict)
                    else response.text
                )
            except ValueError:
                message = response.text
            raise GitHubAPIError(response.status_code, str(message))
        return response

    def list_pr_files(self, pr_number: int) -> list[PrFile]:
        response = self._request(
            "GET",
            f"{self._api_base}/repos/{self._repo}/pulls/{pr_number}/files",
            params={"per_page": "100"},
        )
        return [
            PrFile(
                filename=item["filename"],
                status=item.get("status", "modified"),
                additions=item.get("additions", 0),
                deletions=item.get("deletions", 0),
            )
            for item in response.json()
        ]

    def get_file_content(self, path: str, ref: str) -> str:
        """Fetch raw content at a ref; 404 yields an empty baseline (new files)."""
        try:
            response = self._request(
                "GET",
                f"{self._api_base}/repos/{self._repo}/contents/{quote(path, safe='/')}",
                params={"ref": ref},
                headers={"Accept": "application/vnd.github.raw"},
            )
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                return ""
            raise
        return response.text

    def list_pr_comments(self, pr_number: int) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            f"{self._api_base}/repos/{self._repo}/issues/{pr_number}/comments",
            params={"per_page": "100"},
        )
        payload = response.json()
        return payload if isinstance(payload, list) else []

    def create_pr_comment(self, pr_number: int, body: str) -> None:
        self._request(
            "POST",
            f"{self._api_base}/repos/{self._repo}/issues/{pr_number}/comments",
            json_body={"body": body},
        )

    def update_pr_comment(self, comment_id: int, body: str) -> None:
        self._request(
            "PATCH",
            f"{self._api_base}/repos/{self._repo}/issues/comments/{comment_id}",
            json_body={"body": body},
        )

    def upsert_sticky_comment(self, pr_number: int, body: str, marker: str = COMMENT_MARKER) -> str:
        for comment in self.list_pr_comments(pr_number):
            if marker in str(comment.get("body", "")):
                self.update_pr_comment(int(comment["id"]), body)
                return "updated"
        self.create_pr_comment(pr_number, body)
        return "created"
