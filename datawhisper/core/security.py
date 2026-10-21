"""AST-based SQL sandbox and read-only guardrails for DataWhisper.

Security model
--------------
Every SQL string that originates from an LLM (or any untrusted caller) must
pass through :func:`validate_sql` before it reaches a database driver. The
validator enforces three independent layers of defense:

1. **Lexical hygiene** - multi-statement payloads and SQL comments are
   rejected outright. Comments are a classic bypass vector
   (``SELECT 1 --; DROP TABLE x``) and add zero analytical value, so the
   sandbox refuses them entirely, even inside string literals.
2. **Structural whitelist** - the statement must parse (via ``sqlglot``)
   into a single AST whose root is a read-only relation (SELECT, set
   operations, parenthesized subquery, EXPLAIN/DESCRIBE). Anything else -
   including dialect ``Command`` nodes that sqlglot cannot classify
   (PRAGMA, ATTACH, COPY, INSTALL, VACUUM, ...) - is denied.
3. **Node-level traversal** - every node in the AST is inspected for
   blocked statement types, dangerous scalar/table functions (file I/O,
   extension loading, environment access, foreign catalog scanners) and
   catalog-qualified references that would escape the attached database
   (e.g. ``secret_db.main.users`` after an ATTACH).

The validator is deliberately *fail-closed*: any parse error, unknown
construct or ambiguity results in rejection, never in execution.
"""

from __future__ import annotations

import re
from typing import Final, Literal

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp

SQLDialect = Literal["sqlite", "duckdb"]

#: Root AST nodes that constitute a read-only, single-relation statement.
_ALLOWED_ROOT_NAMES: Final = (
    "Select",
    "Union",
    "Except",
    "Intersect",
    "Subquery",
    "Explain",
    "Describe",
)

#: Statement nodes that mutate state, escape the database or the host.
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
    "Analyze",
    "Vacuum",
    "Checkpoint",
)

#: Functions able to read/write the filesystem, load native code, read the
#: process environment or attach foreign database systems. Blocked regardless
#: of how deeply they nest inside an expression tree.
_BLOCKED_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        # Filesystem / external format readers (DuckDB table functions)
        "read_csv",
        "read_csv_auto",
        "read_json",
        "read_json_auto",
        "read_parquet",
        "read_xlsx",
        "parquet_scan",
        "parquet_metadata",
        "parquet_schema",
        "sniff_csv",
        "glob",
        "file_search",
        "read_text",
        "read_blob",
        # Writers
        "write_csv",
        "write_parquet",
        "write_json",
        # Extension / environment / IPC escape hatches
        "load_extension",
        "getenv",
        "shell",
        "system",
        "copy_from_database",
        # Foreign catalog scanners and attach helpers
        "postgres_scan",
        "postgres_attach",
        "mysql_scan",
        "sqlite_scan",
        "sqlite_attach",
        "iceberg_scan",
        "delta_scan",
    }
)

#: Catalog (ATTACH target) and schema qualifiers the sandbox tolerates.
#: Anything else in a dotted reference means the query reaches outside the
#: database it was opened against.
_ALLOWED_CATALOGS: Final = frozenset({"", "main", "memory"})
_ALLOWED_SCHEMAS: Final = frozenset({"", "main", "memory", "temp", "information_schema"})

_COMMENT_MARKERS: Final = ("--", "/*", "*/")


def _resolve(node_names: tuple[str, ...]) -> tuple[type[exp.Expression], ...]:
    """Resolve sqlglot node class names defensively across sqlglot versions."""
    resolved = []
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
    """Outcome of sandbox validation; ``sql`` is the normalized safe text."""

    ok: bool = False
    dialect: SQLDialect = "sqlite"
    sql: str = ""
    root_kind: str = ""
    violations: list[ValidationViolation] = Field(default_factory=list)


class SQLSecurityError(ValueError):
    """Raised by :func:`validate_or_raise` when the sandbox rejects a statement."""

    def __init__(self, sql: str, violations: list[ValidationViolation]) -> None:
        self.sql = sql
        self.violations = violations
        reasons = "; ".join(f"[{v.code}] {v.message}" for v in violations)
        super().__init__(f"SQL rejected by sandbox: {reasons}")


def _lexical_checks(text: str) -> list[ValidationViolation]:
    """Reject multi-statement payloads and any comment token.

    A single trailing semicolon is tolerated (copy-pasted SQL), but any other
    semicolon - interior or doubled - is treated as statement stacking.
    """
    violations: list[ValidationViolation] = []
    if not text:
        violations.append(
            ValidationViolation(layer="lexical", code="EMPTY", message="statement is empty")
        )
        return violations

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

    ``ReadCSV`` -> ``read_csv`` and ``ReadXLSX`` -> ``read_xlsx`` (a naive
    per-capital split would produce ``read_c_s_v`` and miss the deny list).
    """
    step = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return re.sub(r"([a-z\d])([A-Z])", r"\1_\2", step).lower()


def _function_names(node: exp.Func) -> set[str]:
    """Collect every identifier a function node could be known by.

    sqlglot parses unknown calls as ``Anonymous`` (``name`` = the call name),
    but many dangerous table functions are *typed* nodes (``read_csv`` parses
    to ``ReadCSV``, whose ``name`` is the filename argument). Matching both
    the anonymous name and the snake_case class name closes that gap.
    """
    names: set[str] = set()
    if isinstance(node, exp.Anonymous):
        names.add(node.name.lower())
    names.add(_camel_to_snake(type(node).__name__))
    return names


def _semantic_checks(root: exp.Expression) -> list[ValidationViolation]:
    """Walk the whole AST denying blocked statements, functions and catalogs."""
    violations: list[ValidationViolation] = []
    for node in root.walk():
        if isinstance(node, _BLOCKED_STATEMENTS):
            kind = type(node).__name__.upper()
            violations.append(
                ValidationViolation(
                    layer="semantic",
                    code="BLOCKED_STATEMENT",
                    message=f"{kind} statements are not permitted",
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
                        message=f"catalog qualifier {node.catalog!r} would escape "
                        "the attached database (ATTACH guard)",
                    )
                )
            if schema not in _ALLOWED_SCHEMAS:
                violations.append(
                    ValidationViolation(
                        layer="semantic",
                        code="FOREIGN_SCHEMA",
                        message=f"schema qualifier {node.db!r} is outside the "
                        "attached database (ATTACH guard)",
                    )
                )
    return violations


def validate_sql(sql: str, dialect: SQLDialect = "sqlite") -> ValidationResult:
    """Validate an untrusted SQL string without executing it. Never raises."""
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
                            message=f"root statement {type(root).__name__} is not a "
                            "read-only relation (SELECT/WITH/set-operation/EXPLAIN)",
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


def validate_or_raise(sql: str, dialect: SQLDialect = "sqlite") -> ValidationResult:
    """Validate and return the normalized SQL, or raise :class:`SQLSecurityError`."""
    result = validate_sql(sql, dialect)
    if not result.ok:
        raise SQLSecurityError(sql, result.violations)
    return result
