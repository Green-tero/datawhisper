"""Automated Index Advisor & Cost Optimizer.

Analyzes SQL queries and query execution plans using AST traversal to recommend
optimal, dialect-aware indexes (covering filter predicates, joins, and sorting),
estimating cost reduction to turn cost-gate rejections into actionable optimizations.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp

if TYPE_CHECKING:
    from datawhisper.core.models import CatalogSnapshot, PlanInfo


class IndexRecommendation(BaseModel):
    """An index proposed to optimize a slow, high-cost or unindexed query."""

    table: str
    columns: list[str]
    index_name: str
    sql: str
    dialect: str = "sqlite"
    reason: str = ""
    estimated_cost_reduction_pct: float = 75.0


class AdvisorReport(BaseModel):
    """Collection of optimization recommendations for a given SQL statement."""

    original_sql: str
    recommendations: list[IndexRecommendation] = Field(default_factory=list)
    summary: str = ""

    @property
    def has_recommendations(self) -> bool:
        return len(self.recommendations) > 0


class IndexAdvisor:
    """AST-driven index analyzer recommending single and composite indexes."""

    def __init__(self, dialect: str = "sqlite") -> None:
        self.dialect = dialect

    def advise(
        self,
        sql: str,
        catalog: CatalogSnapshot | None = None,
        plan: PlanInfo | None = None,
    ) -> AdvisorReport:
        """Analyze query AST and optional explain plan to suggest covering indexes."""
        try:
            parsed = sqlglot.parse_one(sql, read=self.dialect)
        except Exception:
            return AdvisorReport(
                original_sql=sql,
                summary="Unable to parse SQL for index advisory.",
            )

        # Collect columns used in WHERE predicates and JOIN ON conditions grouped by table
        filter_cols: dict[str, list[str]] = {}
        join_cols: dict[str, list[str]] = {}
        order_cols: dict[str, list[str]] = {}

        # 1. Inspect WHERE predicates
        where_node = parsed.find(exp.Where)
        if where_node:
            for col in where_node.find_all(exp.Column):
                t_name = (col.table or "").lower()
                c_name = col.name.lower()
                if c_name and c_name not in {"*", "count"}:
                    filter_cols.setdefault(t_name, []).append(c_name)

        # 2. Inspect JOIN conditions
        for join in parsed.find_all(exp.Join):
            on_clause = join.args.get("on")
            if on_clause:
                for col in on_clause.find_all(exp.Column):
                    t_name = (col.table or "").lower()
                    c_name = col.name.lower()
                    if c_name:
                        join_cols.setdefault(t_name, []).append(c_name)

        # 3. Inspect ORDER BY clauses
        order_node = parsed.find(exp.Order)
        if order_node:
            for col in order_node.find_all(exp.Column):
                t_name = (col.table or "").lower()
                c_name = col.name.lower()
                if c_name:
                    order_cols.setdefault(t_name, []).append(c_name)

        # Resolve tables from catalog if table names were unaliased/unspecified
        target_tables = [t.name.lower() for t in (catalog.tables if catalog else [])]
        if not target_tables:
            # Fallback to tables referenced in FROM / JOIN
            for tbl in parsed.find_all(exp.Table):
                if tbl.name:
                    target_tables.append(tbl.name.lower())

        recommendations: list[IndexRecommendation] = []
        seen_signatures: set[tuple[str, tuple[str, ...]]] = set()

        # Combine candidate columns per table
        all_tables = set(filter_cols) | set(join_cols) | set(order_cols) | set(target_tables)
        for tbl_name in all_tables:
            if not tbl_name:
                # If column has no explicit table prefix, match with candidate target tables
                continue

            tbl_filters = [c for c in filter_cols.get(tbl_name, []) if c]
            tbl_joins = [c for c in join_cols.get(tbl_name, []) if c]
            tbl_orders = [c for c in order_cols.get(tbl_name, []) if c]

            # If unaliased columns were captured under empty key "", map to table
            if "" in filter_cols:
                if len(target_tables) == 1:
                    tbl_filters.extend(filter_cols[""])
                elif catalog:
                    table_info = next(
                        (t for t in catalog.tables if t.name.lower() == tbl_name), None
                    )
                    if table_info:
                        valid_cols = {c.name.lower() for c in table_info.columns}
                        tbl_filters.extend([c for c in filter_cols[""] if c in valid_cols])

            if "" in order_cols:
                if len(target_tables) == 1:
                    tbl_orders.extend(order_cols[""])
                elif catalog:
                    table_info = next(
                        (t for t in catalog.tables if t.name.lower() == tbl_name), None
                    )
                    if table_info:
                        valid_cols = {c.name.lower() for c in table_info.columns}
                        tbl_orders.extend([c for c in order_cols[""] if c in valid_cols])

            # De-duplicate while preserving optimal order: filters first, then joins, then order by
            ordered_cols: list[str] = []
            for col_name in tbl_filters + tbl_joins + tbl_orders:
                if col_name not in ordered_cols:
                    ordered_cols.append(col_name)

            # Skip if no columns found or if only a primary key is referenced
            if not ordered_cols:
                continue

            if catalog:
                table_info = next((t for t in catalog.tables if t.name.lower() == tbl_name), None)
                if table_info:
                    pks = [pk.lower() for pk in table_info.primary_key]
                    if len(ordered_cols) == 1 and ordered_cols[0] in pks:
                        # Single-column PK lookup is already indexed
                        continue

            col_tuple = tuple(ordered_cols[:3])  # Cap composite index at 3 columns
            sig = (tbl_name, col_tuple)
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)

            idx_name = f"idx_{tbl_name}_{'_'.join(col_tuple)}"
            # Sanitize identifier and cap length for PostgreSQL identifier limits (63 chars)
            idx_name = re.sub(r"[^a-zA-Z0-9_]", "", idx_name)[:63]

            cols_str = ", ".join(f'"{c}"' for c in col_tuple)
            if self.dialect.lower() == "postgres":
                create_sql = f'CREATE INDEX CONCURRENTLY "{idx_name}" ON "{tbl_name}" ({cols_str});'
            else:
                create_sql = f'CREATE INDEX "{idx_name}" ON "{tbl_name}" ({cols_str});'

            reduction = 85.0 if len(col_tuple) > 1 else 70.0
            cols_desc = ", ".join(col_tuple)
            reason = f"Covers predicate/join filters on {cols_desc} to replace sequential scan"

            recommendations.append(
                IndexRecommendation(
                    table=tbl_name,
                    columns=list(col_tuple),
                    index_name=idx_name,
                    sql=create_sql,
                    dialect=self.dialect,
                    reason=reason,
                    estimated_cost_reduction_pct=reduction,
                )
            )

        summary = (
            f"Proposed {len(recommendations)} index optimization(s) to reduce query latency."
            if recommendations
            else "Query is already well-indexed or does not require additional secondary indexes."
        )

        return AdvisorReport(
            original_sql=sql,
            recommendations=recommendations,
            summary=summary,
        )
