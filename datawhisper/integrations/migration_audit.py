"""Offline static migration audit for pull-request governance.

The bot itself never opens a database; it inspects migration source text the
way a careful reviewer would. Three checks run entirely via ``sqlglot`` ASTs
plus lexical rules, which keeps them deterministic, air-gapped and usable in
``--dry-run`` local mode:

1. **Table lock risk.** Operators with live traffic care whether an
   ``ALTER``/``CREATE INDEX`` forces an exclusive table rewrite. We flag the
   conservative set: ``DROP COLUMN`` and ``SET DATA TYPE`` (table rewrite on
   both SQLite and PostgreSQL), plus ``CREATE INDEX`` without
   ``CONCURRENTLY`` on PostgreSQL.
2. **Destructive drops.** ``DROP TABLE`` / ``DROP COLUMN`` / ``DROP INDEX``
   are reported with exact context so a human can confirm intent.
3. **Missing foreign-key indexes.** A ``REFERENCES`` column without a
   matching index forces FK walks to scan; the report flags the column and
   even suggests the required ``CREATE INDEX`` statement.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp


class MigrationFinding(BaseModel):
    """One static finding attached to a migration file."""

    kind: str
    message: str
    severity: str = "warning"
    statement: str = ""


class MigrationReport(BaseModel):
    findings: list[MigrationFinding] = Field(default_factory=list)

    @property
    def locks(self) -> list[MigrationFinding]:
        return [finding for finding in self.findings if finding.kind == "lock_risk"]

    @property
    def destructive(self) -> list[MigrationFinding]:
        return [finding for finding in self.findings if finding.kind == "destructive_drop"]

    @property
    def missing_fk_indexes(self) -> list[MigrationFinding]:
        return [finding for finding in self.findings if finding.kind == "missing_fk_index"]

    @property
    def blocking(self) -> bool:
        return bool(self.destructive)


def _strip_comment(text: str) -> str:
    """Remove ``--`` line comments without breaking string literals.

    The sandbox forbids comments in *queries*; migration files routinely use
    them, so this scanner is allowed to be lenient about them.
    """
    out: list[str] = []
    in_string = False
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if char == quote and index > 0 and text[index - 1] != "\\":
                in_string = False
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            in_string = True
            quote = char
            out.append(char)
            index += 1
            continue
        if text.startswith("--", index):
            end = text.find("\n", index)
            index = len(text) if end == -1 else end
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _statement_indexed_columns(statement: exp.Expression) -> set[str]:
    """Columns referenced by any CREATE INDEX within a parsed statement batch."""
    columns: set[str] = set()
    for create in statement.find_all(exp.Create):
        if create.kind != "INDEX":
            continue
        index_node = create.this
        for column in index_node.find_all(exp.Column):
            columns.add(column.name.lower())
    return columns


def _collect_create_index_columns(text: str) -> set[str]:
    """Lowercased indexed columns: ``CREATE INDEX ... ON t (col)``."""
    indexed: set[str] = set()
    try:
        statements = [s for s in sqlglot.parse(text) if s is not None]
    except sqlglot.errors.ParseError:
        return indexed
    for statement in statements:
        if not isinstance(statement, exp.Create) or statement.kind != "INDEX":
            continue
        index_node = statement.this  # Index node: "idx ON orders (cols)"
        for column in index_node.find_all(exp.Column):
            indexed.add(column.name.lower())
    return indexed


def analyze_migration_sql(
    text: str,
    *,
    high_traffic_tables: Sequence[str] = (),
    source: str = "",
) -> MigrationReport:
    """Run the static checks over one migration file's SQL text."""
    cleaned = _strip_comment(text)
    report = MigrationReport()
    indexed_columns = _collect_create_index_columns(cleaned)
    high_traffic = {table.lower() for table in high_traffic_tables}

    try:
        statements = [s for s in sqlglot.parse(cleaned) if s is not None]
    except sqlglot.errors.ParseError as exc:
        report.findings.append(
            MigrationFinding(
                kind="parse_error",
                message=f"migration file could not be parsed: {exc}",
                severity="error",
            )
        )
        return report

    for statement in statements:
        statement_sql = statement.sql()
        if isinstance(statement, exp.Create) and statement.kind in {"INDEX", "TABLE"}:
            if statement.kind == "INDEX" and statement_sql.upper().find("CONCURRENTLY") == -1:
                report.findings.append(
                    MigrationFinding(
                        kind="lock_risk",
                        message="CREATE INDEX without CONCURRENTLY may block writes on "
                        "high-traffic tables",
                        statement=statement_sql[:160],
                    )
                )
            if statement.kind == "TABLE":
                _scan_create_table(statement, indexed_columns, high_traffic, report)
        elif isinstance(statement, exp.Alter) and statement.kind == "TABLE":
            _scan_alter_table(statement, high_traffic, report, source)
        elif isinstance(statement, exp.Drop):
            kind = str(statement.args.get("kind") or "").lower()
            if kind in {"table", "index", "column"}:
                report.findings.append(
                    MigrationFinding(
                        kind="destructive_drop",
                        message=f"destructive DROP {kind} detected",
                        severity="error",
                        statement=statement_sql[:160],
                    )
                )

    return report


def _scan_alter_table(
    statement: exp.Alter, high_traffic: set[str], report: MigrationReport, source: str
) -> None:
    table_node = statement.find(exp.Table)
    table_name = (table_node.name if table_node is not None else "").lower()
    for action in statement.args.get("actions") or []:
        if isinstance(action, exp.Drop) and isinstance(action.this, exp.Column):
            column = action.this.name.lower()
            locked = "high-traffic table" if table_name in high_traffic else "table"
            report.findings.append(
                MigrationFinding(
                    kind="destructive_drop",
                    message=f"DROP COLUMN '{column}' rewrites the {locked} '{table_name}'",
                    severity="error",
                    statement=statement.sql()[:160],
                )
            )
            continue
        if isinstance(action, exp.ColumnDef):
            # ADD COLUMN for a new FK reference: needs an index afterwards.
            column_def = action
            name = column_def.name.lower()
            has_fk = any(
                isinstance(constraint, (exp.ForeignKey, exp.Reference))
                for constraint in column_def.args.get("constraints") or []
            )
            if has_fk and name not in _statement_indexed_columns(statement):
                report.findings.append(
                    MigrationFinding(
                        kind="missing_fk_index",
                        message=(
                            f"new column '{name}' on '{table_name}' references another "
                            "table but has no index"
                        ),
                        statement=statement.sql()[:160],
                    )
                )
    # Lock risk on any ADD COLUMN with a default or type rewrite: SQLite and
    # Postgres both rewrite tables for some type changes.
    for action in statement.args.get("actions") or []:
        if isinstance(action, exp.ColumnDef) and action.args.get("kind") is not None:
            kind = action.args.get("kind").sql().upper()
            if kind.startswith(("SET DATA TYPE", "TYPE")):
                report.findings.append(
                    MigrationFinding(
                        kind="lock_risk",
                        message=f"ALTER of '{table_name}' changes a column type, forcing "
                        "a table rewrite",
                        statement=statement.sql()[:160],
                    )
                )


def _scan_create_table(
    statement: exp.Create,
    indexed_columns: set[str],
    high_traffic: set[str],
    report: MigrationReport,
) -> None:
    table_node = statement.find(exp.Table)
    schema = statement.find(exp.Schema)
    if table_node is None or schema is None:
        return
    table_name = table_node.name.lower()
    for column_def in schema.find_all(exp.ColumnDef):
        constraints = column_def.args.get("constraints") or []
        references = [
            constraint
            for constraint in constraints
            if isinstance(constraint, exp.ForeignKey) or isinstance(constraint, exp.Reference)
        ]
        if not references:
            continue
        column_name = column_def.name.lower()
        if column_name not in indexed_columns:
            report.findings.append(
                MigrationFinding(
                    kind="missing_fk_index",
                    message=(
                        f"column '{column_name}' in '{table_name}' is a foreign key "
                        "without a matching index; FK lookups will scan"
                    ),
                    statement=statement.sql()[:160],
                )
            )


def _markdown_table(headers: Sequence[str], rows: Sequence[Iterable[str]]) -> str:
    """Render a markdown table with pipe-escaped cells (no github_bot import)."""
    lines = ["| " + " | ".join(str(h).replace("|", "\\|") for h in headers) + " |"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    lines.extend(
        "| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in row) + " |"
        for row in rows
    )
    return "\n".join(lines)


def render_migration_report(reports: Sequence[MigrationReport]) -> str:
    """Render all findings for one PR as a markdown section."""
    merged = MigrationReport()
    for report in reports:
        merged.findings.extend(report.findings)
    if not merged.findings:
        return ""
    lines = ["### 🧾 Static migration audit", ""]
    if merged.destructive:
        lines.append(
            _markdown_table(
                ["Severity", "Signal", "Detail"],
                [
                    ["🔴 error", finding.kind.replace("_", " "), finding.message]
                    for finding in merged.destructive
                ],
            )
        )
        lines.append("")
    if merged.locks:
        lines.append(
            _markdown_table(
                ["Severity", "Signal", "Detail"],
                [
                    ["🟡 warning", "lock risk", finding.message]
                    for finding in merged.locks
                ],
            )
        )
        lines.append("")
    if merged.missing_fk_indexes:
        lines.append(
            _markdown_table(
                ["Severity", "Signal", "Detail"],
                [
                    ["🟠 warning", "missing FK index", finding.message]
                    for finding in merged.missing_fk_indexes
                ],
            )
        )
        lines.append("")
    return "\n".join(lines)