"""Native Semantic Layer & Data Contracts for governed database copilot operations.

The semantic layer bridges natural language to precise, pre-approved SQL metrics
(metrics, dimensions, grains, filters), preventing model hallucinations on core
business metrics.

The data contracts system enables declarative schema and data quality verification
directly inside CI/CD workflows and audit pipelines.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from datawhisper.core.database import ExecutionEngine


# ---------------------------------------------------------------------------
# Semantic Layer: Dimensions, Metrics and Models
# ---------------------------------------------------------------------------
DimensionType = Literal["categorical", "timestamp", "numeric", "boolean"]


class DimensionDefinition(BaseModel):
    """A business dimension describing a slice of data."""

    name: str
    expression: str
    description: str = ""
    dim_type: DimensionType = "categorical"


class MetricDefinition(BaseModel):
    """A governed business metric with its canonical SQL aggregation formula."""

    name: str
    expression: str
    table: str
    description: str = ""
    filter: str | None = None
    dimensions: list[str] = Field(default_factory=list)
    grain: str | None = None

    def sql_snippet(self) -> str:
        """Render a self-contained SQL query computing this metric."""
        where_clause = f" WHERE {self.filter}" if self.filter else ""
        return f"SELECT {self.expression} AS {self.name} FROM {self.table}{where_clause}"


class SemanticModel(BaseModel):
    """An analytical entity binding physical tables to governed dimensions and metrics."""

    name: str
    table: str
    description: str = ""
    primary_key: list[str] = Field(default_factory=list)
    dimensions: list[DimensionDefinition] = Field(default_factory=list)
    metrics: list[MetricDefinition] = Field(default_factory=list)

    def get_metric(self, name: str) -> MetricDefinition | None:
        lowered = name.lower()
        return next((m for m in self.metrics if m.name.lower() == lowered), None)


class SemanticCatalog(BaseModel):
    """A registry of governed semantic models and standalone metrics."""

    models: list[SemanticModel] = Field(default_factory=list)
    standalone_metrics: list[MetricDefinition] = Field(default_factory=list)

    @property
    def all_metrics(self) -> list[MetricDefinition]:
        all_m = list(self.standalone_metrics)
        for model in self.models:
            all_m.extend(model.metrics)
        return all_m

    def get_metric(self, name: str) -> MetricDefinition | None:
        lowered = name.lower()
        for metric in self.all_metrics:
            if metric.name.lower() == lowered:
                return metric
        return None

    def resolve_metric_for_query(self, query: str) -> MetricDefinition | None:
        """Find the best matching governed metric for a user question."""
        q_lower = query.lower()
        # Direct exact match
        for metric in self.all_metrics:
            m_name = metric.name.lower().replace("_", " ")
            if m_name in q_lower or metric.name.lower() in q_lower:
                return metric
        # Keyword token match
        best_metric: MetricDefinition | None = None
        best_overlap = 0
        q_tokens = set(q_lower.split())
        for metric in self.all_metrics:
            m_tokens = set(metric.name.lower().replace("_", " ").split())
            overlap = len(q_tokens & m_tokens)
            if overlap > best_overlap:
                best_overlap = overlap
                best_metric = metric
        return best_metric if best_overlap > 0 else None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SemanticCatalog:
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, text: str) -> SemanticCatalog:
        return cls.model_validate(json.loads(text))


# ---------------------------------------------------------------------------
# Data Contracts: Declarative Data Governance & CI Quality Assertions
# ---------------------------------------------------------------------------
class ContractRuleType(StrEnum):
    NOT_NULL = "not_null"
    UNIQUE = "unique"
    RANGE = "range"
    ALLOWED_VALUES = "allowed_values"
    ROW_COUNT = "row_count"


class ContractRule(BaseModel):
    """One verifiable assertion on a table or column."""

    column: str = ""
    rule_type: ContractRuleType
    min_value: float | None = None
    max_value: float | None = None
    allowed_values: list[Any] | None = None
    min_rows: int | None = None
    max_rows: int | None = None


class TableContract(BaseModel):
    """A collection of rules that define the data contract for one table."""

    table: str
    rules: list[ContractRule] = Field(default_factory=list)


class ContractViolation(BaseModel):
    """A detected deviation from the expected data contract."""

    table: str
    column: str
    rule_type: str
    message: str
    failing_count: int = 0


class DataContractReport(BaseModel):
    """Comprehensive summary of data contract evaluation."""

    table: str
    passed: bool
    checked_rules: int = 0
    violations: list[ContractViolation] = Field(default_factory=list)


def verify_table_contract(engine: ExecutionEngine, contract: TableContract) -> DataContractReport:
    """Execute non-destructive verification queries against an engine for a table contract."""
    violations: list[ContractViolation] = []
    checked = 0
    table = contract.table

    for rule in contract.rules:
        checked += 1
        rtype = rule.rule_type

        if rtype == ContractRuleType.ROW_COUNT:
            sql = f'SELECT COUNT(*) FROM "{table}"'
            count = int(engine.execute(sql).rows[0][0])
            if rule.min_rows is not None and count < rule.min_rows:
                violations.append(
                    ContractViolation(
                        table=table,
                        column="*",
                        rule_type=rtype.value,
                        message=f"Row count {count} is less than expected minimum {rule.min_rows}",
                        failing_count=rule.min_rows - count,
                    )
                )
            if rule.max_rows is not None and count > rule.max_rows:
                violations.append(
                    ContractViolation(
                        table=table,
                        column="*",
                        rule_type=rtype.value,
                        message=f"Row count {count} exceeds expected maximum {rule.max_rows}",
                        failing_count=count - rule.max_rows,
                    )
                )

        elif rtype == ContractRuleType.NOT_NULL:
            col = rule.column
            sql = f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" IS NULL'
            null_count = int(engine.execute(sql).rows[0][0])
            if null_count > 0:
                violations.append(
                    ContractViolation(
                        table=table,
                        column=col,
                        rule_type=rtype.value,
                        message=f"Found {null_count} unexpected NULL values in column '{col}'",
                        failing_count=null_count,
                    )
                )

        elif rtype == ContractRuleType.UNIQUE:
            col = rule.column
            sql = (
                f"SELECT COUNT(*) FROM ("
                f'SELECT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL '
                f'GROUP BY "{col}" HAVING COUNT(*) > 1) s'
            )
            dup_count = int(engine.execute(sql).rows[0][0])
            if dup_count > 0:
                violations.append(
                    ContractViolation(
                        table=table,
                        column=col,
                        rule_type=rtype.value,
                        message=(
                            f"Found {dup_count} duplicate values violating "
                            f"uniqueness on '{col}'"
                        ),
                        failing_count=dup_count,
                    )
                )

        elif rtype == ContractRuleType.RANGE:
            col = rule.column
            clauses: list[str] = []
            if rule.min_value is not None:
                clauses.append(f'"{col}" < {rule.min_value}')
            if rule.max_value is not None:
                clauses.append(f'"{col}" > {rule.max_value}')
            if clauses:
                cond = " OR ".join(clauses)
                sql = f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" IS NOT NULL AND ({cond})'
                out_of_bounds = int(engine.execute(sql).rows[0][0])
                if out_of_bounds > 0:
                    bounds = f"[{rule.min_value}, {rule.max_value}]"
                    violations.append(
                        ContractViolation(
                            table=table,
                            column=col,
                            rule_type=rtype.value,
                            message=(
                                f"Found {out_of_bounds} values outside range "
                                f"{bounds} on '{col}'"
                            ),
                            failing_count=out_of_bounds,
                        )
                    )

        elif rtype == ContractRuleType.ALLOWED_VALUES:
            col = rule.column
            allowed = rule.allowed_values or []
            if allowed:
                formatted_values = ", ".join(
                    f"'{v}'" if isinstance(v, str) else str(v) for v in allowed
                )
                sql = (
                    f'SELECT COUNT(*) FROM "{table}" '
                    f'WHERE "{col}" IS NOT NULL AND "{col}" NOT IN ({formatted_values})'
                )
                disallowed_count = int(engine.execute(sql).rows[0][0])
                if disallowed_count > 0:
                    violations.append(
                        ContractViolation(
                            table=table,
                            column=col,
                            rule_type=rtype.value,
                            message=(
                                f"Found {disallowed_count} values outside allowed "
                                f"set on '{col}'"
                            ),
                            failing_count=disallowed_count,
                        )
                    )

    return DataContractReport(
        table=table,
        passed=len(violations) == 0,
        checked_rules=checked,
        violations=violations,
    )
