"""Zero-trust SQL guardrails and the cost-based execution gatekeeper.

Security invariants
-------------------
1. **Fail closed.** Any parse error, unclassified statement or ambiguity is a
   rejection, never an execution.
2. **Read-only by construction.** The root must be a SELECT, a set operation,
   a parenthesized subquery, or EXPLAIN. Mutating nodes are denied even when
   nested (a ``DELETE`` hidden under a CTE is still a ``DELETE`` node).
3. **No host access.** File readers, extension loaders, environment accessors
   and foreign-catalog scans are denied by function name *and* by node class,
   because sqlglot parses many dangerous DuckDB table functions into typed
   nodes (``read_csv`` -> ``ReadCSV``) whose ``.name`` is the argument, not
   the function.
4. **Bounded cost.** Before execution the planner is asked for a plan; the
   gate rejects cartesian products and paginates large unbounded scans.

The lexical layer (no semicolons, no comment tokens) is intentionally stricter
than SQL requires. Comments add no analytical value here and are a classic
bypass vector, so refusing them outright removes a whole class of parser
differential attacks.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final, Literal

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp

from datawhisper.core.config import CostPolicy
from datawhisper.core.models import PlanInfo

SQLDialect = Literal["sqlite", "duckdb", "postgres"]

_ALLOWED_ROOT_NAMES: Final = (
    "Select",
    "Union",
    "Except",
    "Intersect",
    "Subquery",
    "Explain",
    "Describe",
)

_BLOCKED_STATEMENT_NAMES: Final = (
    "Command",
    "Pragma",
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Create",
    "CreateIndex",
    "CreateView",
    "Drop",
    "Alter",
    "AlterTable",
    "Attach",
    "Detach",
    "Copy",
    "Install",
    "Load",
    "Use",
    "Set",
    "Grant",
    "Revoke",
    "Transaction",
    "Commit",
    "Rollback",
    "Analyze",
    "Vacuum",
    "Checkpoint",
)

#: Functions that touch the filesystem, load native code, read the process
#: environment, reach foreign systems or burn CPU/session state server-side.
_BLOCKED_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        # DuckDB readers/writers
        "read_csv",
        "read_csv_auto",
        "read_json",
        "read_json_auto",
        "read_ndjson",
        "read_ndjson_auto",
        "read_parquet",
        "read_xlsx",
        "parquet_scan",
        "parquet_metadata",
        "parquet_schema",
        "sniff_csv",
        "read_text",
        "read_blob",
        "write_csv",
        "write_parquet",
        "write_json",
        "glob",
        "file_search",
        # Extensions, environment, process control
        "load_extension",
        "getenv",
        "shell",
        "system",
        "copy_from_database",
        # Foreign catalogs
        "postgres_scan",
        "postgres_attach",
        "mysql_scan",
        "postgres_query",
        "sqlite_scan",
        "sqlite_attach",
        "iceberg_scan",
        "delta_scan",
        # Filesystem / server-side I/O in PostgreSQL
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        # Resource exhaustion / session tampering
        "pg_sleep",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "set_config",
        "pg_advisory_lock",
        # DuckDB catalog/system introspection. duckdb_secrets() exposes
        # credential material from SECRET objects, so it is denied.
        "duckdb_secrets",
        "duckdb_settings",
        "duckdb_extensions",
        "duckdb_views",
        "duckdb_tables",
        "duckdb_columns",
        "duckdb_constraints",
        "duckdb_functions",
        "duckdb_databases",
        "duckdb_dependencies",
        "duckdb_indexes",
        "duckdb_memory",
        "duckdb_temporary_files",
        "duckdb_storage_info",
        "current_setting",
    }
)

_ALLOWED_CATALOGS: Final = frozenset({"", "main", "memory"})
_ALLOWED_SCHEMAS: Final = frozenset({"", "main", "memory", "temp", "information_schema", "public"})
_COMMENT_MARKERS: Final = ("--", "/*", "*/")


def _resolve(node_names: Iterable[str]) -> tuple[type[exp.Expression], ...]:
    """Resolve sqlglot node class names defensively across versions."""
    resolved: list[type[exp.Expression]] = []
    for name in node_names:
        node = getattr(exp, name, None)
        if isinstance(node, type) and issubclass(node, exp.Expression):
            resolved.append(node)
    return tuple(resolved)


_ALLOWED_ROOTS: Final = _resolve(_ALLOWED_ROOT_NAMES)
_BLOCKED_STATEMENTS: Final = _resolve(_BLOCKED_STATEMENT_NAMES)


class ValidationViolation(BaseModel):
    """A single sandbox rejection reason, kept structured for telemetry."""

    layer: Literal["lexical", "structural", "semantic"] = "semantic"
    code: str
    message: str


class ValidationResult(BaseModel):
    """Outcome of sandbox validation; ``sql`` is normalized and semicolon-free."""

    ok: bool = False
    dialect: str = "sqlite"
    sql: str = ""
    root_kind: str = ""
    violations: list[ValidationViolation] = Field(default_factory=list)


class SQLSecurityError(ValueError):
    """Raised when the sandbox rejects a statement."""

    def __init__(self, sql: str, violations: list[ValidationViolation]) -> None:
        self.sql = sql
        self.violations = violations
        reasons = "; ".join(f"[{v.code}] {v.message}" for v in violations)
        super().__init__(f"SQL rejected by sandbox: {reasons}")


class CostViolation(RuntimeError):
    """Raised when the cost gate refuses a statement (or cannot bound it)."""

    def __init__(self, decision: CostDecision) -> None:
        self.decision = decision
        reasons = "; ".join(decision.reasons) or "policy rejection"
        super().__init__(f"SQL rejected by cost gate: {reasons}")


class CostDecision(BaseModel):
    """Gatekeeper verdict, including the rewritten (paginated) SQL if any."""

    allowed: bool = False
    action: Literal["allow", "limit", "reject"] = "reject"
    reasons: list[str] = Field(default_factory=list)
    rewritten_sql: str | None = None
    estimated_scan_rows: float = 0.0
    cartesian_groups: list[list[str]] = Field(default_factory=list)
    plan: PlanInfo | None = None


# ---------------------------------------------------------------------------
# Lexical + structural validation
# ---------------------------------------------------------------------------
def _lexical_checks(text: str) -> list[ValidationViolation]:
    violations: list[ValidationViolation] = []
    if not text:
        return [ValidationViolation(layer="lexical", code="EMPTY", message="statement is empty")]
    if ";" in text:
        violations.append(
            ValidationViolation(
                layer="lexical",
                code="MULTI_STATEMENT",
                message="semicolons are not permitted (multi-statement guard)",
            )
        )
    for marker in _COMMENT_MARKERS:
        if marker in text:
            violations.append(
                ValidationViolation(
                    layer="lexical",
                    code="COMMENT_TOKEN",
                    message=f"comment token {marker!r} is not permitted "
                    "(strict policy: applies even inside string literals)",
                )
            )
    return violations


def _camel_to_snake(name: str) -> str:
    """Convert sqlglot class names to SQL names, handling acronym runs.

    ``ReadCSV`` -> ``read_csv``; a naive per-capital split would produce
    ``read_c_s_v`` and silently miss the deny list.
    """
    step = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return re.sub(r"([a-z\d])([A-Z])", r"\1_\2", step).lower()


def _function_names(node: exp.Func) -> set[str]:
    names: set[str] = set()
    if isinstance(node, exp.Anonymous):
        names.add(node.name.lower())
    names.add(_camel_to_snake(type(node).__name__))
    return names


def _semantic_checks(root: exp.Expression) -> list[ValidationViolation]:
    violations: list[ValidationViolation] = []
    for node in root.walk():
        if isinstance(node, _BLOCKED_STATEMENTS):
            violations.append(
                ValidationViolation(
                    layer="semantic",
                    code="BLOCKED_STATEMENT",
                    message=f"{type(node).__name__.upper()} statements are not permitted",
                )
            )
            continue
        if isinstance(node, exp.Func):
            blocked = _function_names(node) & _BLOCKED_FUNCTIONS
            if blocked:
                violations.append(
                    ValidationViolation(
                        layer="semantic",
                        code="BLOCKED_FUNCTION",
                        message=f"function {sorted(blocked)[0]!r} is not permitted "
                        "(filesystem/extension/environment access)",
                    )
                )
        if isinstance(node, exp.Table):
            catalog = (node.catalog or "").lower()
            schema = (node.db or "").lower()
            if catalog not in _ALLOWED_CATALOGS:
                violations.append(
                    ValidationViolation(
                        layer="semantic",
                        code="FOREIGN_CATALOG",
                        message=f"catalog qualifier {node.catalog!r} would escape the "
                        "attached database (ATTACH guard)",
                    )
                )
            if schema not in _ALLOWED_SCHEMAS:
                violations.append(
                    ValidationViolation(
                        layer="semantic",
                        code="FOREIGN_SCHEMA",
                        message=f"schema qualifier {node.db!r} is outside the attached "
                        "database (ATTACH guard)",
                    )
                )
    return violations


def validate_sql(sql: str, dialect: str = "sqlite") -> ValidationResult:
    """Validate untrusted SQL without executing it. Never raises."""
    text = sql.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()

    violations = _lexical_checks(text)
    root: exp.Expression | None = None

    if not violations:
        try:
            statements = [s for s in sqlglot.parse(text, read=dialect) if s is not None]
        except sqlglot.errors.ParseError as exc:
            violations.append(
                ValidationViolation(
                    layer="structural",
                    code="PARSE_ERROR",
                    message=f"statement does not parse as {dialect} SQL: {exc}",
                )
            )
        else:
            if len(statements) != 1:
                violations.append(
                    ValidationViolation(
                        layer="structural",
                        code="MULTI_STATEMENT",
                        message=f"expected exactly one statement, parsed {len(statements)}",
                    )
                )
            else:
                root = statements[0]
                if not isinstance(root, _ALLOWED_ROOTS):
                    violations.append(
                        ValidationViolation(
                            layer="structural",
                            code="NON_READ_ONLY_ROOT",
                            message=f"root statement {type(root).__name__} is not a read-only "
                            "relation (SELECT/WITH/set-operation/EXPLAIN)",
                        )
                    )

    if root is not None and not violations:
        violations.extend(_semantic_checks(root))

    return ValidationResult(
        ok=not violations,
        dialect=dialect,
        sql=text if not violations else "",
        root_kind=type(root).__name__ if root is not None else "",
        violations=violations,
    )


def validate_or_raise(sql: str, dialect: str = "sqlite") -> ValidationResult:
    """Validate and return normalized SQL, or raise :class:`SQLSecurityError`."""
    result = validate_sql(sql, dialect)
    if not result.ok:
        raise SQLSecurityError(sql, result.violations)
    return result


def transpile_sql(sql: str, read: str, write: str) -> str:
    """Validate under the source dialect, then emit equivalent target SQL.

    Validation happens *before* transpilation so a payload cannot exploit
    cross-dialect parsing differences to smuggle a blocked construct.
    """
    validated = validate_or_raise(sql, read)
    tree = sqlglot.parse_one(validated.sql, read=read)
    return str(tree.sql(dialect=write))


# ---------------------------------------------------------------------------
# LIMIT helpers
# ---------------------------------------------------------------------------
def current_limit(sql: str, dialect: str = "sqlite") -> int | None:
    """Return the literal top-level LIMIT, or ``None`` when unbounded/non-literal."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return None
    # find() would return a LIMIT inside a subquery, which does not bound the
    # outer result set. Only the root clause bounds the statement.
    limit = tree.args.get("limit")
    if not isinstance(limit, exp.Limit):
        return None
    expression = limit.expression
    if isinstance(expression, exp.Literal) and expression.is_int:
        return int(expression.this)
    return None


def apply_limit(sql: str, dialect: str, limit: int) -> str | None:
    """Attach or tighten a top-level LIMIT; ``None`` if the shape forbids it."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return None
    existing = tree.args.get("limit")
    if isinstance(existing, exp.Limit):
        expression = existing.expression
        if (
            isinstance(expression, exp.Literal)
            and expression.is_int
            and int(expression.this) <= limit
        ):
            return sql
        try:
            existing.set("expression", exp.Literal.number(limit))
            return str(tree.sql(dialect=dialect))
        except (AttributeError, ValueError):
            return None
    try:
        # sqlglot exposes limit() on Select/Union at runtime; the base
        # Expression type used by the parser does not declare it.
        bounded = tree.limit(limit)  # type: ignore[attr-defined]
        return str(bounded.sql(dialect=dialect))
    except (AttributeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Cartesian-product detection (dialect independent, AST based)
# ---------------------------------------------------------------------------
class _UnionFind:
    """Small union-find over query-scope table aliases."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self._parent.setdefault(item, item)

    def find(self, item: str) -> str:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: str, right: str) -> None:
        self.add(left)
        self.add(right)
        root_left, root_right = self.find(left), self.find(right)
        if root_left != root_right:
            self._parent[root_right] = root_left

    def groups(self) -> list[list[str]]:
        buckets: dict[str, list[str]] = {}
        for item in self._parent:
            buckets.setdefault(self.find(item), []).append(item)
        return list(buckets.values())


def _scope_tables(select: exp.Select) -> dict[str, str]:
    """Map alias -> table name for tables owned by *this* SELECT scope."""
    mapping: dict[str, str] = {}
    for table in select.find_all(exp.Table):
        if table.find_ancestor(exp.Select) is not select:
            continue
        mapping[(table.alias or table.name).lower()] = table.name.lower()
    return mapping


def _aliases_in(node: exp.Expression, scope: set[str]) -> set[str]:
    aliases: set[str] = set()
    for column in node.find_all(exp.Column):
        qualifier = (column.table or "").lower()
        if qualifier and qualifier in scope:
            aliases.add(qualifier)
    return aliases


def extract_table_names(sql: str, dialect: str = "sqlite") -> list[str]:
    """Return referenced table names (lowercased) for planner-less cost checks."""
    try:
        statement = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return []
    return sorted({table.name.lower() for table in statement.find_all(exp.Table) if table.name})


def detect_cartesian_joins(sql: str, dialect: str = "sqlite") -> list[list[str]]:
    """Return disconnected table groups per SELECT scope.

    A group with more than one member means the query combines tables without
    any connecting predicate in that scope - an implicit CROSS JOIN. Explicit
    ``CROSS JOIN`` with a connecting WHERE predicate is *not* flagged, because
    the planner will still use an equijoin.
    """
    try:
        statement = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return []

    offenders: list[list[str]] = []
    for select in statement.find_all(exp.Select):
        tables = _scope_tables(select)
        if len(tables) < 2:
            continue
        scope = set(tables)
        union = _UnionFind()
        for alias in scope:
            union.add(alias)

        from_clause = select.args.get("from")
        previous: str | None = None
        if isinstance(from_clause, exp.From) and isinstance(from_clause.this, exp.Expression):
            previous = (from_clause.this.alias_or_name or "").lower() or None
        for join in select.args.get("joins") or []:
            if not isinstance(join, exp.Join):
                continue
            target = join.this
            joined = (
                (target.alias_or_name or "").lower() if isinstance(target, exp.Expression) else ""
            )
            on_clause = join.args.get("on")
            if isinstance(on_clause, exp.Expression):
                linked = _aliases_in(on_clause, scope)
                if len(linked) >= 2:
                    ordered = sorted(linked)
                    for alias in ordered[1:]:
                        union.union(ordered[0], alias)
            elif join.args.get("using") or join.args.get("method") == "NATURAL":
                # USING/NATURAL joins are joins by definition; connect the new
                # table to its left neighbour instead of flagging a cartesian.
                if previous in scope and joined in scope:
                    union.union(previous, joined)
            if joined:
                previous = joined

        where = select.args.get("where")
        if isinstance(where, exp.Expression):
            for predicate in where.find_all(exp.Binary):
                if predicate.find_ancestor(exp.Select) is not select:
                    continue
                linked = _aliases_in(predicate, scope)
                if len(linked) >= 2:
                    ordered = sorted(linked)
                    for alias in ordered[1:]:
                        union.union(ordered[0], alias)

        groups = union.groups()
        if len(groups) > 1:
            offenders.append(
                sorted(
                    f"{tables[alias]} ({alias})" if alias != tables[alias] else tables[alias]
                    for group in groups
                    for alias in group
                )
            )
    return offenders


# ---------------------------------------------------------------------------
# Cost gatekeeper
# ---------------------------------------------------------------------------
class CostGatekeeper:
    """Decision layer between AST validation and execution.

    The gate deliberately does not trust planner estimates alone: when the
    planner (notably SQLite) omits cardinalities, catalog row counts are used
    as a conservative substitute. This keeps the policy meaningful across all
    supported engines without engine-specific code here.
    """

    def __init__(self, policy: CostPolicy | None = None) -> None:
        self.policy = policy or CostPolicy()

    def evaluate(
        self,
        sql: str,
        plan: PlanInfo,
        table_rows: dict[str, int] | None = None,
    ) -> CostDecision:
        policy = self.policy
        if not policy.enabled:
            return CostDecision(
                allowed=True, action="allow", plan=plan, reasons=["cost gate disabled"]
            )

        rows = table_rows or {}
        reasons: list[str] = []

        if plan.error is not None:
            if policy.plan_failure_policy == "reject":
                return CostDecision(
                    allowed=False,
                    action="reject",
                    reasons=[f"planner unavailable and policy is fail-closed: {plan.error}"],
                    plan=plan,
                )
            reasons.append(f"planner unavailable; relying on hard timeouts/row caps: {plan.error}")

        groups = detect_cartesian_joins(sql, plan.dialect)
        if groups and policy.reject_cartesian_joins:
            return CostDecision(
                allowed=False,
                action="reject",
                reasons=[
                    "cartesian join detected: tables combined without connecting predicate",
                    *("disconnected group: " + ", ".join(group) for group in groups),
                ],
                cartesian_groups=groups,
                plan=plan,
            )

        if plan.cross_join and policy.reject_cartesian_joins:
            return CostDecision(
                allowed=False,
                action="reject",
                reasons=["planner reports a CROSS JOIN operator"],
                plan=plan,
            )

        estimated = plan.estimated_scan_rows
        if estimated <= 0:
            # Engines such as SQLite omit cardinalities. Fall back to catalog
            # row counts, and when the plan exposed no nodes at all, to the
            # tables named in the AST - otherwise an empty plan would silently
            # look free.
            scan_tables = plan.scan_tables or extract_table_names(sql, plan.dialect)
            estimated = float(sum(rows.get(name, 0) for name in scan_tables))

        if estimated > policy.max_scan_rows:
            reasons.append(
                f"estimated full-scan rows {estimated:.0f} exceed budget {policy.max_scan_rows}"
            )
            if policy.force_limit_for_large_scans:
                existing = current_limit(sql, plan.dialect)
                if existing is not None and existing <= policy.auto_limit:
                    reasons.append(
                        f"statement is already bounded to {existing} rows; scan cost accepted"
                    )
                    return CostDecision(
                        allowed=True,
                        action="allow",
                        reasons=reasons,
                        estimated_scan_rows=estimated,
                        plan=plan,
                    )
                rewritten = apply_limit(sql, plan.dialect, policy.auto_limit)
                if rewritten is not None and rewritten != sql:
                    reasons.append(f"forced pagination: LIMIT {policy.auto_limit} injected")
                    return CostDecision(
                        allowed=True,
                        action="limit",
                        reasons=reasons,
                        rewritten_sql=rewritten,
                        estimated_scan_rows=estimated,
                        plan=plan,
                    )
                return CostDecision(
                    allowed=False,
                    action="reject",
                    reasons=[*reasons, "could not inject LIMIT into this statement shape"],
                    estimated_scan_rows=estimated,
                    plan=plan,
                )
            return CostDecision(
                allowed=False,
                action="reject",
                reasons=reasons,
                estimated_scan_rows=estimated,
                plan=plan,
            )

        return CostDecision(
            allowed=True,
            action="allow",
            reasons=reasons or ["plan within budget"],
            estimated_scan_rows=estimated,
            plan=plan,
        )
