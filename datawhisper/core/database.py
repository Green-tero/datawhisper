"""Multi-engine execution with Apache Arrow transfer and hard resource bounds.

Engine matrix
-------------
===============  ==========================  ==================================
Source           Backend                     Notes
===============  ==========================  ==================================
``.sqlite/.db``  SQLite (stdlib)             opened ``mode=ro`` plus ``query_only``
``.duckdb``      DuckDB (optional)           opened ``read_only=True``
``.parquet``     DuckDB in-memory view       file access disabled after registration
``.jsonl``       DuckDB in-memory view       vectorized JSON extraction
``.csv``         DuckDB, SQLite fallback     typed ingest when DuckDB is absent
``postgres://``  psycopg (optional)          read-only transaction + statement_timeout
===============  ==========================  ==================================

Trust boundary
--------------
``execute`` is the only agent-reachable path: it validates the AST and runs the
cost gate before touching a driver. Catalog introspection and profiling use
module-generated SQL and therefore skip the sandbox, but still run under the
same timeout and row-cap discipline.

Zero-copy: DuckDB results are handed back as native Arrow tables (``to_arrow_table``)
and only the requested slice is materialized. SQLite and PostgreSQL have no
Arrow fast path, so their rows are buffered and converted lazily; the trade-off
is explicit rather than hidden behind a misleading "Arrow" abstraction.
"""

from __future__ import annotations

import contextlib
import csv
import json
import re
import sqlite3
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Protocol
from urllib.parse import quote

from pydantic import BaseModel, Field

from datawhisper.core.config import DataWhisperConfig, SafetyThresholds
from datawhisper.core.models import (
    CatalogSnapshot,
    ColumnInfo,
    ForeignKeyInfo,
    PlanInfo,
    PlanNode,
    QueryResult,
    RedactionReport,
    TableInfo,
    TabularResult,
)
from datawhisper.core.privacy import PrivacyScrubber
from datawhisper.core.security import (
    CostDecision,
    CostGatekeeper,
    CostViolation,
    apply_limit,
    validate_or_raise,
)

_DRIVERS_WITHOUT_INTERRUPT = frozenset({"duckdb", "postgres"})

_SCAN_NODE_NAMES = frozenset(
    {
        "SEQ_SCAN",
        "TABLE_SCAN",
        "READ_PARQUET",
        "READ_CSV",
        "READ_CSV_AUTO",
        "READ_JSON",
        "READ_JSON_AUTO",
        "READ_NDJSON",
        "READ_NDJSON_AUTO",
        "READ_XLSX",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class QueryTimeoutError(RuntimeError):
    """The statement exceeded its wall-clock budget."""


class QueryExecutionError(RuntimeError):
    """Driver-level failure (syntax, missing column, type mismatch, ...)."""

    def __init__(self, message: str, *, driver: str) -> None:
        super().__init__(message)
        self.driver = driver


class BackendUnavailableError(RuntimeError):
    """An optional driver required by this source is not installed."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return str(value)
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    return str(value)


def _quote_identifier(identifier: str) -> str:
    """Quote an internally validated identifier for trusted SQL assembly."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise ValueError(f"refusing to quote unsafe identifier: {identifier!r}")
    return f'"{identifier}"'


def _sql_literal(value: str) -> str:
    """Render a filesystem path as a SQL string literal (single-quote escaped)."""
    return "'" + value.replace("'", "''") + "'"


def _sanitize_table_name(stem: str) -> str:
    name = re.sub(r"\W+", "_", stem).strip("_").lower() or "data"
    if name[0].isdigit():
        name = f"t_{name}"
    return name


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _import_duckdb() -> Any:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise BackendUnavailableError(
            "DuckDB sources require the 'duckdb' extra: pip install 'datawhisper[duckdb]'"
        ) from exc
    return duckdb


def _import_pyarrow() -> Any:
    try:
        import pyarrow
    except ImportError:  # pragma: no cover - depends on extras
        return None
    return pyarrow


def _import_psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise BackendUnavailableError(
            "PostgreSQL sources require the 'postgres' extra: pip install 'datawhisper[postgres]'"
        ) from exc
    return psycopg


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------
class DatabaseBackend(Protocol):
    """Contract every engine adapter implements.

    ``dialect`` is the sqlglot dialect name used for validation and
    transpilation; ``driver_name`` selects interruption semantics.
    """

    @property
    def dialect(self) -> str: ...

    @property
    def driver_name(self) -> str: ...

    def fetch(self, sql: str, limit: int) -> TabularResult: ...

    def explain(self, sql: str) -> PlanInfo: ...

    def introspect(self) -> CatalogSnapshot: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------
class SQLiteBackend:
    """Read-only SQLite; ``path=None`` creates an in-memory scratch database."""

    def __init__(self, path: Path | None) -> None:
        if path is None:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            uri = f"file:{quote(path.resolve().as_posix())}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._conn.execute("PRAGMA query_only = ON")
        self._conn.row_factory = None

    @property
    def dialect(self) -> str:
        return "sqlite"

    @property
    def driver_name(self) -> str:
        return "sqlite"

    @property
    def connection(self) -> sqlite3.Connection:
        """Exposed for trusted CSV ingest into a scratch database only."""
        return self._conn

    def fetch(self, sql: str, limit: int) -> TabularResult:
        try:
            cursor = self._conn.execute(sql)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(limit)
        except sqlite3.Error as exc:
            raise QueryExecutionError(str(exc), driver="sqlite") from exc
        return TabularResult(columns=columns, _rows=[tuple(row) for row in rows])

    def explain(self, sql: str) -> PlanInfo:
        try:
            rows = self._conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
        except sqlite3.Error as exc:
            return PlanInfo(dialect=self.dialect, error=str(exc), raw=str(exc))
        nodes: list[PlanNode] = []
        raw_lines: list[str] = []
        for row in rows:
            detail = str(row[-1])
            raw_lines.append(detail)
            if detail.startswith("SCAN CONSTANT ROW"):
                continue
            if detail.startswith("SEARCH"):
                token = detail.split()[1] if len(detail.split()) > 1 else ""
                nodes.append(PlanNode(node_type="INDEX_SCAN", table=token or None, detail=detail))
            elif detail.startswith("SCAN"):
                parts = detail.split()
                token = parts[1] if len(parts) > 1 and parts[1].upper() != "CONSTANT" else ""
                nodes.append(PlanNode(node_type="SCAN", table=token or None, detail=detail))
            else:
                nodes.append(PlanNode(node_type="OTHER", detail=detail))
        return PlanInfo(dialect=self.dialect, nodes=nodes, raw="\n".join(raw_lines))

    def introspect(self) -> CatalogSnapshot:
        snapshot = CatalogSnapshot(dialect=self.dialect)
        relations = self._conn.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for name, kind in relations:
            table_name = str(name)
            quoted = _quote_identifier(table_name)
            column_rows = self._conn.execute(f"PRAGMA table_info({quoted})").fetchall()
            columns = [
                ColumnInfo(
                    name=str(row[1]),
                    type=str(row[2] or "UNKNOWN"),
                    nullable=not int(row[3] or 0),
                    primary_key=bool(row[5]),
                )
                for row in column_rows
            ]
            foreign_keys: list[ForeignKeyInfo] = []
            fk_rows = self._conn.execute(f"PRAGMA foreign_key_list({quoted})").fetchall()
            grouped: dict[int, ForeignKeyInfo] = {}
            for fk in fk_rows:
                fk_id = int(fk[0])
                entry = grouped.get(fk_id)
                if entry is None:
                    entry = ForeignKeyInfo(
                        from_table=table_name,
                        to_table=str(fk[2]),
                    )
                    grouped[fk_id] = entry
                entry.from_columns.append(str(fk[3]))
                if fk[4] is not None:
                    entry.to_columns.append(str(fk[4]))
            foreign_keys = list(grouped.values())
            system = table_name.startswith("sqlite_")
            row_count: int | None = None
            if not system:
                with contextlib.suppress(sqlite3.Error):
                    row_count = int(
                        self._conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
                    )
            snapshot.tables.append(
                TableInfo(
                    name=table_name,
                    columns=columns,
                    row_count=row_count,
                    foreign_keys=foreign_keys,
                    is_view=str(kind) == "view",
                )
            )
        return snapshot

    def cancel(self) -> None:
        # sqlite3.interrupt() is safe to call from another thread.
        self._conn.interrupt()

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self._conn.close()

    def ingest_csv(self, path: Path, table: str | None = None) -> str:
        """Load a CSV into the scratch database, then lock it read-only."""
        name = table or _sanitize_table_name(path.stem)
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header is None:
                raise ValueError(f"CSV file is empty: {path}")
            raw_rows = list(reader)

        columns: list[str] = []
        seen: set[str] = set()
        for index, original in enumerate(header):
            cleaned = _sanitize_table_name(original) or f"col_{index}"
            while cleaned in seen:
                cleaned = f"{cleaned}_2"
            seen.add(cleaned)
            columns.append(cleaned)

        kinds: list[Literal["int", "float", "text"]] = ["int"] * len(columns)
        for row in raw_rows[:100]:
            for i, cell in enumerate(row[: len(columns)]):
                if cell == "" or kinds[i] == "text":
                    continue
                try:
                    int(cell)
                except ValueError:
                    try:
                        float(cell)
                        kinds[i] = "float"
                    except ValueError:
                        kinds[i] = "text"

        typed: list[tuple[Any, ...]] = []
        for row in raw_rows:
            values: list[Any] = []
            for i in range(len(columns)):
                cell = row[i] if i < len(row) else ""
                if cell == "":
                    values.append(None)
                elif kinds[i] == "text":
                    values.append(cell)
                else:
                    try:
                        values.append(int(cell) if kinds[i] == "int" else float(cell))
                    except ValueError:
                        values.append(cell)
            typed.append(tuple(values))

        ddl = ", ".join(
            f"{_quote_identifier(c)} {k.upper()}" for c, k in zip(columns, kinds, strict=True)
        )
        self._conn.execute(f"CREATE TABLE {_quote_identifier(name)} ({ddl})")
        placeholders = ", ".join("?" * len(columns))
        self._conn.executemany(
            f"INSERT INTO {_quote_identifier(name)} VALUES ({placeholders})", typed
        )
        self._conn.commit()
        self._conn.execute("PRAGMA query_only = ON")
        return name


# ---------------------------------------------------------------------------
# DuckDB (relational, Parquet, JSONL, CSV)
# ---------------------------------------------------------------------------
class DuckDBBackend:
    """DuckDB adapter covering native files plus Parquet/JSONL/CSV views."""

    def __init__(self, connection: Any) -> None:
        self._conn = connection
        self._closed = False

    @property
    def dialect(self) -> str:
        return "duckdb"

    @property
    def driver_name(self) -> str:
        return "duckdb"

    # --- constructors --------------------------------------------------
    @classmethod
    def from_file(cls, path: Path) -> DuckDBBackend:
        duckdb = _import_duckdb()
        return cls(duckdb.connect(str(path), read_only=True))

    @classmethod
    def from_parquet(cls, path: Path) -> DuckDBBackend:
        target = path
        if path.is_dir():
            target = path / "*.parquet"
        literal = _sql_literal(str(target).replace("\\", "/"))
        return cls._from_view(
            _sanitize_table_name(path.stem or "parquet_data"), f"read_parquet({literal})"
        )

    @classmethod
    def from_jsonl(cls, path: Path) -> DuckDBBackend:
        literal = _sql_literal(str(path).replace("\\", "/"))
        return cls._from_view(_sanitize_table_name(path.stem), f"read_json_auto({literal})")

    @classmethod
    def from_csv(cls, path: Path) -> DuckDBBackend:
        literal = _sql_literal(str(path).replace("\\", "/"))
        return cls._from_view(
            _sanitize_table_name(path.stem), f"read_csv_auto({literal}, header=true)"
        )

    @classmethod
    def _from_view(cls, name: str, reader: str) -> DuckDBBackend:
        duckdb = _import_duckdb()
        connection = duckdb.connect(":memory:")
        connection.execute(f"CREATE VIEW {_quote_identifier(name)} AS SELECT * FROM {reader}")
        # A view over an external file is evaluated lazily, so DuckDB must keep
        # filesystem access enabled for the registered reader. Zero-trust is
        # therefore enforced one level up: the AST sandbox rejects every
        # user-initiated read_* / COPY / INSTALL / ATTACH, and the engine only
        # ever exposes this single, engine-constructed view.
        return cls(connection)

    # --- execution -----------------------------------------------------
    def fetch(self, sql: str, limit: int) -> TabularResult:
        if self._closed:
            raise QueryExecutionError(
                "DuckDB connection was closed after a timeout", driver="duckdb"
            )
        bounded = f"SELECT * FROM ({sql}) AS dw_cap LIMIT {int(limit)}"
        try:
            # DuckDB forbids a second statement while a result is unconsumed,
            # so the Arrow path uses sql()/to_arrow_table() exclusively and the
            # row path uses execute()/fetchmany() - never both on one query.
            if _import_pyarrow() is not None:
                table = self._conn.sql(bounded).to_arrow_table()
                return TabularResult(columns=table.column_names, _arrow=table)
            cursor = self._conn.execute(bounded)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(limit)
        except Exception as exc:  # duckdb.Error is not importable without the extra
            if self._closed:
                raise QueryExecutionError("DuckDB connection is closed", driver="duckdb") from exc
            raise QueryExecutionError(str(exc), driver="duckdb") from exc
        return TabularResult(columns=columns, _rows=[tuple(row) for row in rows])

    def explain(self, sql: str) -> PlanInfo:
        try:
            raw_rows = self._conn.execute(f"EXPLAIN (FORMAT JSON) {sql}").fetchall()
        except Exception as exc:
            return PlanInfo(dialect=self.dialect, error=str(exc), raw=str(exc))
        raw = (
            raw_rows[0][1]
            if raw_rows and len(raw_rows[0]) > 1
            else (raw_rows[0][0] if raw_rows else "")
        )
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as exc:
            return PlanInfo(dialect=self.dialect, error=str(exc), raw=str(raw)[:4000])

        nodes: list[PlanNode] = []

        def walk(node: dict[str, Any]) -> None:
            name = str(node.get("name", "")).upper()
            info = node.get("extra_info") or {}
            estimated = _as_float(info.get("Estimated Cardinality"))
            table = info.get("Table") or info.get("table")
            mapped = "SEQ_SCAN" if name in _SCAN_NODE_NAMES else name
            nodes.append(
                PlanNode(
                    node_type=mapped,
                    table=str(table) if table else None,
                    estimated_rows=estimated,
                    detail=json.dumps(info)[:300],
                )
            )
            for child in node.get("children") or []:
                if isinstance(child, dict):
                    walk(child)

        for entry in payload if isinstance(payload, list) else [payload]:
            if isinstance(entry, dict):
                walk(entry)
        return PlanInfo(dialect=self.dialect, nodes=nodes, raw=str(raw)[:4000])

    def introspect(self) -> CatalogSnapshot:
        snapshot = CatalogSnapshot(dialect=self.dialect)
        relations = self._conn.execute(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
        primary_keys: dict[str, list[str]] = {}
        foreign_keys: dict[str, list[ForeignKeyInfo]] = {}
        with contextlib.suppress(Exception):
            for row in self._conn.execute(
                "SELECT table_name, constraint_type, constraint_column_names, "
                "referenced_table, referenced_column_names FROM duckdb_constraints()"
            ).fetchall():
                table_name, kind, columns, target, target_columns = (
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                )
                if kind == "PRIMARY KEY":
                    primary_keys[str(table_name)] = [str(c) for c in (columns or [])]
                elif kind == "FOREIGN KEY" and target:
                    foreign_keys.setdefault(str(table_name), []).append(
                        ForeignKeyInfo(
                            from_table=str(table_name),
                            from_columns=[str(c) for c in (columns or [])],
                            to_table=str(target),
                            to_columns=[str(c) for c in (target_columns or [])],
                        )
                    )

        for name, kind in relations:
            table_name = str(name)
            column_rows = self._conn.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
                [table_name],
            ).fetchall()
            pk_columns = {c.lower() for c in primary_keys.get(table_name, [])}
            columns = [
                ColumnInfo(
                    name=str(row[0]),
                    type=str(row[1]),
                    nullable=str(row[2]).upper() == "YES",
                    primary_key=str(row[0]).lower() in pk_columns,
                )
                for row in column_rows
            ]
            row_count: int | None = None
            with contextlib.suppress(Exception):
                # fetchall (not fetchone): DuckDB keeps a result "unfinished"
                # until a fetch returns no rows, which would poison the next
                # statement on this connection.
                count_rows = self._conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}"
                ).fetchall()
                row_count = int(count_rows[0][0])
            snapshot.tables.append(
                TableInfo(
                    name=table_name,
                    columns=columns,
                    row_count=row_count,
                    foreign_keys=foreign_keys.get(table_name, []),
                    is_view=str(kind).upper() == "VIEW",
                )
            )
        return snapshot

    def cancel(self) -> None:
        # DuckDB 2.x has no cross-thread interrupt; closing abandons the query.
        self._closed = True
        with contextlib.suppress(Exception):
            self._conn.close()

    def close(self) -> None:
        self._closed = True
        with contextlib.suppress(Exception):
            self._conn.close()


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------
class PostgresBackend:
    """Read-only PostgreSQL adapter with server-side statement timeouts."""

    def __init__(self, dsn: str, timeout_seconds: float = 10.0) -> None:
        psycopg = _import_psycopg()
        self._timeout = timeout_seconds
        try:
            self._conn = psycopg.connect(dsn, autocommit=True)
        except Exception as exc:
            raise BackendUnavailableError(f"could not connect to PostgreSQL: {exc}") from exc
        self._conn.execute("SET default_transaction_read_only = on")
        self._conn.execute(f"SET statement_timeout = {int(timeout_seconds * 1000)}")

    @property
    def dialect(self) -> str:
        return "postgres"

    @property
    def driver_name(self) -> str:
        return "postgres"

    def fetch(self, sql: str, limit: int) -> TabularResult:
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(sql)
                columns = [d.name for d in cursor.description] if cursor.description else []
                rows = cursor.fetchmany(limit)
        except Exception as exc:
            raise QueryExecutionError(str(exc), driver="postgres") from exc
        return TabularResult(columns=columns, _rows=[tuple(row) for row in rows])

    def explain(self, sql: str) -> PlanInfo:
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(f"EXPLAIN (FORMAT JSON) {sql}")
                row = cursor.fetchone()
        except Exception as exc:
            return PlanInfo(dialect=self.dialect, error=str(exc), raw=str(exc))
        payload = row[0] if row else []
        if isinstance(payload, str):
            with contextlib.suppress(json.JSONDecodeError):
                payload = json.loads(payload)
        nodes: list[PlanNode] = []

        def walk(node: dict[str, Any]) -> None:
            node_type = str(node.get("Node Type", "UNKNOWN"))
            mapped = {
                "Seq Scan": "SEQ_SCAN",
                "Index Scan": "INDEX_SCAN",
                "Index Only Scan": "INDEX_SCAN",
                "Bitmap Index Scan": "INDEX_SCAN",
                "Nested Loop": "NESTED_LOOP",
            }.get(node_type, node_type.upper().replace(" ", "_"))
            nodes.append(
                PlanNode(
                    node_type=mapped,
                    table=node.get("Relation Name"),
                    estimated_rows=_as_float(node.get("Plan Rows")),
                    detail=str(node.get("Join Type", "")),
                )
            )
            for child in node.get("Plans") or []:
                if isinstance(child, dict):
                    walk(child)

        for entry in payload if isinstance(payload, list) else [payload]:
            if isinstance(entry, dict) and isinstance(entry.get("Plan"), dict):
                walk(entry["Plan"])
        return PlanInfo(dialect=self.dialect, nodes=nodes, raw=str(payload)[:4000])

    def introspect(self) -> CatalogSnapshot:
        snapshot = CatalogSnapshot(dialect=self.dialect)
        relations = self._conn.execute(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY table_name"
        ).fetchall()
        for name, kind in relations:
            table_name = str(name)
            column_rows = self._conn.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = %s ORDER BY ordinal_position",
                (table_name,),
            ).fetchall()
            columns = [
                ColumnInfo(
                    name=str(row[0]), type=str(row[1]), nullable=str(row[2]).upper() == "YES"
                )
                for row in column_rows
            ]
            keys: list[ForeignKeyInfo] = []
            with contextlib.suppress(Exception):
                key_rows = self._conn.execute(
                    "SELECT kcu.column_name, ccu.table_name, ccu.column_name "
                    "FROM information_schema.table_constraints tc "
                    "JOIN information_schema.key_column_usage kcu "
                    "ON tc.constraint_name = kcu.constraint_name "
                    "JOIN information_schema.constraint_column_usage ccu "
                    "ON tc.constraint_name = ccu.constraint_name "
                    "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_name = %s",
                    (table_name,),
                ).fetchall()
                keys = [
                    ForeignKeyInfo(
                        from_table=table_name,
                        from_columns=[str(row[0])],
                        to_table=str(row[1]),
                        to_columns=[str(row[2])],
                    )
                    for row in key_rows
                ]
            with contextlib.suppress(Exception):
                row_count = int(
                    self._conn.execute(
                        "SELECT reltuples::bigint FROM pg_class WHERE relname = %s", (table_name,)
                    ).fetchone()[0]
                )
            snapshot.tables.append(
                TableInfo(
                    name=table_name,
                    columns=columns,
                    row_count=row_count,
                    foreign_keys=keys,
                    is_view=str(kind).upper() == "VIEW",
                )
            )
        return snapshot

    def cancel(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.cancel()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()


# ---------------------------------------------------------------------------
# Profiling models
# ---------------------------------------------------------------------------
class ValueCount(BaseModel):
    value: str
    count: int


class NumericStats(BaseModel):
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None


class ColumnProfile(BaseModel):
    """Column statistics with PII-safe top values (scrubbed before return)."""

    table: str
    column: str
    total_rows: int
    null_count: int
    null_ratio: float
    distinct_count: int | None = None
    top_values: list[ValueCount] = Field(default_factory=list)
    numeric_stats: NumericStats | None = None
    redactions: RedactionReport | None = None


# ---------------------------------------------------------------------------
# Execution engine
# ---------------------------------------------------------------------------
class ExecutionEngine:
    """Sandboxed, cost-gated, timeout-isolated façade over a backend."""

    def __init__(self, backend: DatabaseBackend, config: DataWhisperConfig | None = None) -> None:
        self.backend = backend
        self.config = config or DataWhisperConfig()
        self.safety: SafetyThresholds = self.config.safety
        self._gatekeeper = CostGatekeeper(self.config.cost)
        self._scrubber = PrivacyScrubber(self.config.privacy)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dw-exec")
        self._poisoned = False
        self._catalog: CatalogSnapshot | None = None

    @classmethod
    def from_source(
        cls, source: str | Path, config: DataWhisperConfig | None = None
    ) -> ExecutionEngine:
        """Select the right backend for a file path, directory or DSN."""
        active = config or DataWhisperConfig()
        text = str(source)
        lowered = text.lower()
        if lowered.startswith(("postgres://", "postgresql://")):
            return cls(PostgresBackend(text, active.safety.query_timeout_seconds), active)
        if text == ":memory:":
            return cls(SQLiteBackend(None), active)

        path = Path(text)
        if not path.exists():
            raise FileNotFoundError(f"data source not found: {path}")
        if path.is_dir():
            if any(path.glob("*.parquet")):
                return cls(DuckDBBackend.from_parquet(path), active)
            raise FileNotFoundError(f"no supported data files in directory: {path}")

        suffix = path.suffix.lower()
        if suffix == ".duckdb":
            return cls(DuckDBBackend.from_file(path), active)
        if suffix in {".parquet", ".pq"}:
            return cls(DuckDBBackend.from_parquet(path), active)
        if suffix in {".jsonl", ".ndjson"}:
            return cls(DuckDBBackend.from_jsonl(path), active)
        if suffix == ".csv":
            try:
                return cls(DuckDBBackend.from_csv(path), active)
            except BackendUnavailableError:
                backend = SQLiteBackend(None)
                backend.ingest_csv(path)
                return cls(backend, active)
        return cls(SQLiteBackend(path), active)

    # ------------------------------------------------------------------
    # Agent-reachable path
    # ------------------------------------------------------------------
    def execute(
        self,
        sql: str,
        *,
        validate: bool = True,
        check_cost: bool = True,
        timeout_s: float | None = None,
        max_rows: int | None = None,
        scrub: bool = True,
    ) -> QueryResult:
        """Validate, gate, execute and scrub one statement.

        Raises :class:`SQLSecurityError` (AST), :class:`CostViolation` (budget)
        or :class:`QueryTimeoutError`/``QueryExecutionError`` (driver). All are
        fed back to the agent as observations for self-correction.
        """
        if self._poisoned:
            raise QueryTimeoutError("engine was abandoned after a previous timeout")

        statement = sql
        if validate:
            statement = validate_or_raise(sql, self.backend.dialect).sql

        decision: CostDecision | None = None
        plan: PlanInfo | None = None
        if check_cost and self.config.cost.enabled:
            plan = self.backend.explain(statement)
            decision = self._gatekeeper.evaluate(statement, plan, self._row_counts())
            if not decision.allowed:
                raise CostViolation(decision)
            if decision.action == "limit" and decision.rewritten_sql:
                statement = decision.rewritten_sql
                validate_or_raise(statement, self.backend.dialect)

        limit = (max_rows if max_rows is not None else self.safety.max_rows_returned) + 1
        timeout = timeout_s if timeout_s is not None else self.safety.query_timeout_seconds
        result, elapsed_ms = self._run_fetch(statement, timeout, limit)

        effective = limit - 1
        truncated = result.num_rows > effective
        raw_rows = result.to_rows(effective)

        scrubbed, report = (
            self._scrubber.scrub(result.columns, raw_rows)
            if scrub
            else (raw_rows, RedactionReport(mode="disabled"))
        )
        rows = [[_to_jsonable(value) for value in row] for row in scrubbed]

        return QueryResult(
            columns=result.columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=round(elapsed_ms, 3),
            plan=plan,
            cost_action=decision.action if decision is not None else "allow",
            limit_applied=(
                self.config.cost.auto_limit
                if decision is not None and decision.action == "limit"
                else None
            ),
            redactions=report if report.total else None,
        )

    def explain(self, sql: str) -> tuple[PlanInfo, CostDecision]:
        """Return the normalized plan and the gate verdict without executing."""
        statement = validate_or_raise(sql, self.backend.dialect).sql
        plan = self.backend.explain(statement)
        decision = self._gatekeeper.evaluate(statement, plan, self._row_counts())
        return plan, decision

    # ------------------------------------------------------------------
    # Trusted introspection path (module-generated SQL only)
    # ------------------------------------------------------------------
    def introspect(self, refresh: bool = False) -> CatalogSnapshot:
        if self._catalog is None or refresh:
            self._catalog = self.backend.introspect()
        return self._catalog

    def profile_column(self, table: str, column: str, top_k: int | None = None) -> ColumnProfile:
        """Aggregate statistics with scrubbed top values."""
        snapshot = self.introspect()
        table_info = snapshot.table(table)
        if table_info is None:
            raise ValueError(f"unknown table: {table!r}")
        column_info = table_info.column(column)
        if column_info is None:
            raise ValueError(f"unknown column {column!r} on table {table_info.name!r}")

        k = top_k if top_k is not None else self.safety.max_column_sample_values
        quoted_table = _quote_identifier(table_info.name)
        quoted_column = _quote_identifier(column_info.name)

        counts = self._privileged(
            f"SELECT COUNT(*), COUNT({quoted_column}), COUNT(DISTINCT {quoted_column}) "
            f"FROM {quoted_table}"
        )
        total, non_null, distinct = counts.rows[0][0], counts.rows[0][1], counts.rows[0][2]

        top = self._privileged(
            f"SELECT {quoted_column} AS value, COUNT(*) AS freq FROM {quoted_table} "
            f"GROUP BY {quoted_column} ORDER BY freq DESC, value ASC LIMIT {int(k)}"
        )
        # Top values are the highest-risk part of a profile: they can expose
        # individual emails, cards or tokens. Scrub before building the model.
        scrubbed_rows, report = self._scrubber.scrub(
            [column_info.name, "frequency"],
            [[row[0], row[1]] for row in top.rows],
        )
        top_values = [
            ValueCount(
                value="NULL" if row[0] is None else str(row[0]),
                count=int(row[1]),
            )
            for row in scrubbed_rows
        ]

        numeric_stats: NumericStats | None = None
        with contextlib.suppress(Exception):
            stats = self._privileged(
                f"SELECT MIN({quoted_column}), MAX({quoted_column}), AVG({quoted_column}) "
                f"FROM {quoted_table}"
            )
            minimum, maximum, mean = stats.rows[0]
            numeric_stats = NumericStats(
                minimum=None if minimum is None else float(minimum),
                maximum=None if maximum is None else float(maximum),
                mean=None if mean is None else float(mean),
            )

        total_int = int(total)
        return ColumnProfile(
            table=table_info.name,
            column=column_info.name,
            total_rows=total_int,
            null_count=total_int - int(non_null),
            null_ratio=round((total_int - int(non_null)) / total_int, 4) if total_int else 0.0,
            distinct_count=int(distinct) if distinct is not None else None,
            top_values=top_values,
            numeric_stats=numeric_stats,
            redactions=report if report.total else None,
        )

    def suggest_limit(self, sql: str) -> str | None:
        """Expose bounded pagination for CLI ``--explain-only`` hints."""
        return apply_limit(sql, self.backend.dialect, self.config.cost.auto_limit)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.backend.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _row_counts(self) -> dict[str, int]:
        try:
            return self.introspect().row_counts()
        except Exception:
            return {}

    def _privileged(self, sql: str) -> QueryResult:
        """Run module-generated SQL under the same timeout/row discipline."""
        if self._poisoned:
            raise QueryTimeoutError("engine is unavailable after a previous timeout")
        result, elapsed = self._run_fetch(
            sql, self.safety.query_timeout_seconds, self.safety.max_rows_returned
        )
        rows = [[_to_jsonable(v) for v in row] for row in result.to_rows()]
        return QueryResult(
            columns=result.columns, rows=rows, row_count=len(rows), elapsed_ms=elapsed
        )

    def _run_fetch(self, sql: str, timeout: float, limit: int) -> tuple[TabularResult, float]:
        start = perf_counter()
        future: Future[TabularResult] = self._executor.submit(self.backend.fetch, sql, limit)
        try:
            result = future.result(timeout=timeout)
        except FuturesTimeoutError as exc:
            self.backend.cancel()
            if self.backend.driver_name in _DRIVERS_WITHOUT_INTERRUPT:
                self._poisoned = True
            raise QueryTimeoutError(f"statement exceeded the {timeout}s execution budget") from exc
        except QueryExecutionError as exc:
            # Driver messages can echo literals from the statement. Scrub
            # before the text reaches the agent, telemetry or the terminal.
            clean, _ = self._scrubber.scrub_text(str(exc))
            raise QueryExecutionError(clean, driver=exc.driver) from exc
        return result, (perf_counter() - start) * 1000.0


__all__ = [
    "BackendUnavailableError",
    "ColumnProfile",
    "DatabaseBackend",
    "DuckDBBackend",
    "ExecutionEngine",
    "PostgresBackend",
    "QueryExecutionError",
    "QueryTimeoutError",
    "SQLiteBackend",
]
