"""Shared data contracts used across the engine.

Kept separate from the backends so the schema graph, agent, reporter and CLI
can depend on one canonical shape without importing driver modules (which
would create cycles and force optional drivers at import time).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
class ColumnInfo(BaseModel):
    """A physical column: type and nullability come from the driver catalog."""

    name: str
    type: str = "UNKNOWN"
    nullable: bool = True
    primary_key: bool = False


class ForeignKeyInfo(BaseModel):
    """A declared (or confidently inferred) foreign-key relationship."""

    from_table: str
    from_columns: list[str] = Field(default_factory=list)
    to_table: str
    to_columns: list[str] = Field(default_factory=list)


class TableInfo(BaseModel):
    """A table or view with its columns, row estimate and outgoing keys."""

    name: str
    columns: list[ColumnInfo] = Field(default_factory=list)
    row_count: int | None = None
    foreign_keys: list[ForeignKeyInfo] = Field(default_factory=list)
    is_view: bool = False

    def column(self, name: str) -> ColumnInfo | None:
        lowered = name.lower()
        return next((c for c in self.columns if c.name.lower() == lowered), None)

    @property
    def primary_key(self) -> list[str]:
        return [c.name for c in self.columns if c.primary_key]


class CatalogSnapshot(BaseModel):
    """Point-in-time catalog used by the graph builder and the cost gate."""

    dialect: str
    tables: list[TableInfo] = Field(default_factory=list)

    def table(self, name: str) -> TableInfo | None:
        lowered = name.lower()
        return next((t for t in self.tables if t.name.lower() == lowered), None)

    def row_counts(self) -> dict[str, int]:
        return {t.name: t.row_count or 0 for t in self.tables}


# ---------------------------------------------------------------------------
# Query plans
# ---------------------------------------------------------------------------
class PlanNode(BaseModel):
    """One normalized plan node, independent of the originating engine."""

    node_type: str
    table: str | None = None
    estimated_rows: float | None = None
    detail: str = ""


class PlanInfo(BaseModel):
    """Normalized plan plus the raw driver output for auditability."""

    dialect: str
    nodes: list[PlanNode] = Field(default_factory=list)
    raw: str = ""
    error: str | None = None

    @property
    def full_scans(self) -> list[PlanNode]:
        return [n for n in self.nodes if n.node_type.upper() in {"SCAN", "SEQ_SCAN", "TABLE_SCAN"}]

    @property
    def scan_tables(self) -> list[str]:
        return [n.table for n in self.full_scans if n.table]

    @property
    def estimated_scan_rows(self) -> float:
        """Sum of estimated rows over full scans; 0.0 when the engine omits estimates."""
        return float(sum(n.estimated_rows or 0.0 for n in self.full_scans))

    @property
    def cross_join(self) -> bool:
        return any(
            "CROSS" in n.node_type.upper() or "CARTESIAN" in n.node_type.upper() for n in self.nodes
        )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
class RedactionReport(BaseModel):
    """Counts-only PII telemetry. Never carries the sensitive values themselves."""

    total: int = 0
    by_category: dict[str, int] = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=list)
    rows_affected: int = 0
    mode: str = "hash"

    def merge(self, other: RedactionReport) -> RedactionReport:
        merged = dict(self.by_category)
        for category, count in other.by_category.items():
            merged[category] = merged.get(category, 0) + count
        return RedactionReport(
            total=self.total + other.total,
            by_category=merged,
            columns=sorted({*self.columns, *other.columns}),
            rows_affected=self.rows_affected + other.rows_affected,
            mode=self.mode,
        )


class QueryResult(BaseModel):
    """Scrubbed, bounded result of one statement."""

    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0
    plan: PlanInfo | None = None
    cost_action: str = "allow"
    limit_applied: int | None = None
    redactions: RedactionReport | None = None


@dataclass(slots=True)
class TabularResult:
    """Driver output before scrubbing.

    Holds either a materialized row buffer or a PyArrow table. When Arrow is
    present the driver-native table is retained so downstream consumers can
    avoid a round-trip through Python objects.
    """

    columns: list[str]
    _rows: list[tuple[Any, ...]] = field(default_factory=list)
    _arrow: Any | None = None

    @property
    def num_rows(self) -> int:
        if self._arrow is not None:
            return int(self._arrow.num_rows)
        return len(self._rows)

    def to_rows(self, limit: int | None = None) -> list[list[Any]]:
        if self._arrow is not None:
            table = self._arrow if limit is None else self._arrow.slice(0, limit)
            return [list(row.values()) for row in table.to_pylist()]
        rows = self._rows if limit is None else self._rows[:limit]
        return [list(row) for row in rows]

    def to_arrow(self) -> Any:
        """Return a PyArrow table, materializing one if the backend lacked Arrow."""
        if self._arrow is not None:
            return self._arrow
        try:
            import pyarrow as pa
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise RuntimeError("Arrow support requires the 'arrow' extra") from exc
        columns = {name: [row[i] for row in self._rows] for i, name in enumerate(self.columns)}
        return pa.table(columns)

    def to_polars(self) -> Any:
        """Convert to Polars when the optional dependency is installed."""
        try:
            import polars as pl
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise RuntimeError("Polars support requires the 'arrow' extra") from exc
        return pl.from_arrow(self.to_arrow())


class LatencyTelemetry(BaseModel):
    """Dual-speed latency split for one autonomous run.

    ``system_1_ms`` covers local decision-engine work (intent, firewall,
    pruning relevance); ``system_2_ms`` covers LLM synthesis; ``execution_ms``
    covers sandboxed SQL runs. Engine-internal statement checks are sub-
    millisecond and included in ``system_1_ms`` when measured agent-side.
    """

    system_1_ms: float = 0.0
    system_2_ms: float = 0.0
    execution_ms: float = 0.0
    decision_calls: int = 0


class AgentTelemetry(BaseModel):
    """One self-healing event, kept for CI diagnostics and dashboards."""

    model_config = ConfigDict(frozen=True)

    stage: str
    outcome: str
    detail: str = ""
