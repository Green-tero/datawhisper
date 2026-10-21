"""Unified SQLite / DuckDB execution engine with timeout isolation.

Trust boundary
--------------
Two execution paths exist and they never mix:

* :meth:`ExecutionEngine.execute` is the **untrusted path**: every call is
  forced through the AST sandbox first and runs under a wall-clock timeout
  with a row cap. This is the only path the agent can reach.
* :meth:`ExecutionEngine._execute_privileged` runs *internally generated*
  SQL (catalog introspection, column profiling, EXPLAIN wrapping). The SQL
  is assembled by this module from validated identifiers, so it skips the
  sandbox but still runs under the same timeout discipline.

Isolation strategy: every statement executes on a single-worker thread pool.
On timeout the backend is interrupted from the caller thread - SQLite has a
purpose-built ``interrupt()``; DuckDB has none, so the connection is closed
(best-effort) and the engine is marked poisoned because DuckDB connections
must not be reused after concurrent close.
"""

from __future__ import annotations

import contextlib
import csv
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

from datawhisper.core.config import SafetyThresholds
from datawhisper.core.security import validate_or_raise


class QueryTimeoutError(RuntimeError):
    """Raised when a statement exceeds its wall-clock budget."""


class QueryExecutionError(RuntimeError):
    """Driver-level failure (syntax, missing column, type mismatch, ...)."""

    def __init__(self, message: str, *, driver: str) -> None:
        super().__init__(message)
        self.driver = driver


class BackendUnavailableError(RuntimeError):
    """The optional driver backing a backend is not installed."""


def _to_jsonable(value: Any) -> Any:
    """Coerce driver-specific scalars into JSON/pydantic-safe primitives."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # Non-finite floats serialize as invalid JSON; keep them visible as text.
        return (
            value if value == value and value not in (float("inf"), float("-inf")) else str(value)
        )
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    return str(value)


class QueryResult(BaseModel):
    """Materialized result of one sandboxed statement."""

    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0


class ColumnInfo(BaseModel):
    name: str
    type: str
    nullable: bool = True


class TableInfo(BaseModel):
    name: str
    columns: list[ColumnInfo] = Field(default_factory=list)
    row_count: int | None = None


class CatalogSnapshot(BaseModel):
    dialect: str
    tables: list[TableInfo] = Field(default_factory=list)


class ValueCount(BaseModel):
    value: str
    count: int


class NumericStats(BaseModel):
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None


class ColumnProfile(BaseModel):
    table: str
    column: str
    total_rows: int
    null_count: int
    null_ratio: float
    distinct_count: int | None = None
    top_values: list[ValueCount] = Field(default_factory=list)
    numeric_stats: NumericStats | None = None


class Backend(Protocol):
    """Minimal contract every execution backend implements."""

    @property
    def dialect(self) -> str: ...

    @property
    def driver_name(self) -> str: ...

    def fetch(self, sql: str, limit: int) -> tuple[list[str], list[tuple[Any, ...]]]: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


def _quote_identifier(identifier: str) -> str:
    """Quote an *internally validated* identifier for trusted SQL assembly."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise ValueError(f"refusing to quote unsafe identifier: {identifier!r}")
    return f'"{identifier}"'


class SQLiteBackend:
    """Read-only SQLite backend; ``path=None`` means an in-memory scratch DB."""

    def __init__(self, path: Path | None) -> None:
        if path is None:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            uri = f"file:{quote(path.resolve().as_posix())}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            # Belt-and-braces: even though the file is opened read-only, enforce
            # statement-level read-only mode so a smuggled write fails loudly.
            self._conn.execute("PRAGMA query_only = ON")

    @property
    def dialect(self) -> str:
        return "sqlite"

    @property
    def driver_name(self) -> str:
        return "sqlite"

    @property
    def connection(self) -> sqlite3.Connection:
        """Exposed solely for the trusted CSV ingest path before locking down."""
        return self._conn

    def fetch(self, sql: str, limit: int) -> tuple[list[str], list[tuple[Any, ...]]]:
        try:
            cursor = self._conn.execute(sql)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(limit)
        except sqlite3.Error as exc:
            raise QueryExecutionError(str(exc), driver="sqlite") from exc
        return columns, [tuple(row) for row in rows]

    def cancel(self) -> None:
        # Thread-safe by design in sqlite3: aborts the in-flight statement.
        self._conn.interrupt()

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self._conn.close()

    def ingest_csv(self, path: Path, table: str | None = None) -> str:
        """Load a CSV into the in-memory scratch DB, then lock it read-only."""
        name = table or re.sub(r"\W+", "_", path.stem).strip("_") or "data"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"unsafe table name derived from file: {name!r}")

        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header is None:
                raise ValueError(f"CSV file is empty: {path}")
            raw_rows = list(reader)

        columns: list[str] = []
        seen: set[str] = set()
        for idx, original in enumerate(header):
            cleaned = re.sub(r"\W+", "_", original).strip("_") or f"col_{idx}"
            if cleaned[0].isdigit():
                cleaned = f"c_{cleaned}"
            while cleaned in seen:
                cleaned = f"{cleaned}_2"
            seen.add(cleaned)
            columns.append(cleaned)

        # Type inference over a sample: int -> float -> text (permissive).
        kinds: list[Literal["int", "float", "text"]] = ["int"] * len(columns)
        for row in raw_rows[:100]:
            for i, cell in enumerate(row[: len(columns)]):
                if cell == "" or kinds[i] == "text":
                    continue
                try:
                    int(cell)
                except ValueError:
                    if cell.strip() == "":
                        continue
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
                elif kinds[i] == "int":
                    try:
                        values.append(int(cell))
                    except ValueError:
                        values.append(cell)
                elif kinds[i] == "float":
                    try:
                        values.append(float(cell))
                    except ValueError:
                        values.append(cell)
                else:
                    values.append(cell)
            typed.append(tuple(values))

        ddl_columns = ", ".join(
            f"{_quote_identifier(c)} {k.upper()}" for c, k in zip(columns, kinds, strict=True)
        )
        self._conn.execute(f"CREATE TABLE {_quote_identifier(name)} ({ddl_columns})")
        placeholders = ", ".join("?" * len(columns))
        self._conn.executemany(
            f"INSERT INTO {_quote_identifier(name)} VALUES ({placeholders})", typed
        )
        self._conn.commit()
        self._conn.execute("PRAGMA query_only = ON")
        return name


class DuckDBBackend:
    """Read-only DuckDB backend; the ``duckdb`` package is an optional extra."""

    def __init__(self, path: Path) -> None:
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise BackendUnavailableError(
                "DuckDB support requires the optional extra: pip install 'datawhisper[duckdb]'"
            ) from exc
        self._duckdb = duckdb
        self._conn = duckdb.connect(str(path), read_only=True)

    @property
    def dialect(self) -> str:
        return "duckdb"

    @property
    def driver_name(self) -> str:
        return "duckdb"

    def fetch(self, sql: str, limit: int) -> tuple[list[str], list[tuple[Any, ...]]]:
        try:
            cursor = self._conn.execute(sql)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(limit)
        except self._duckdb.Error as exc:
            raise QueryExecutionError(str(exc), driver="duckdb") from exc
        return columns, [tuple(row) for row in rows]

    def cancel(self) -> None:
        # DuckDB has no cross-thread interrupt; closing abandons the query and
        # poisons the connection (the engine enforces that).
        with contextlib.suppress(self._duckdb.Error):
            self._conn.close()

    def close(self) -> None:
        with contextlib.suppress(self._duckdb.Error):
            self._conn.close()


class ExecutionEngine:
    """Sandboxed, timeout-isolated façade over a backend driver."""

    def __init__(
        self,
        backend: Backend,
        safety: SafetyThresholds | None = None,
    ) -> None:
        self.backend = backend
        self.safety = safety or SafetyThresholds()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dw-exec")
        self._poisoned = False

    @classmethod
    def from_source(
        cls,
        source: str | Path,
        safety: SafetyThresholds | None = None,
    ) -> ExecutionEngine:
        """Build an engine from a file path, URL-style path, or ``:memory:``."""
        text = str(source)
        if text == ":memory:":
            return cls(SQLiteBackend(None), safety)
        path = Path(text)
        if not path.is_file():
            raise FileNotFoundError(f"data source not found: {path}")
        suffix = path.suffix.lower()
        if suffix == ".duckdb":
            return cls(DuckDBBackend(path), safety)
        if suffix == ".csv":
            backend = SQLiteBackend(None)
            backend.ingest_csv(path)
            return cls(backend, safety)
        # .db / .sqlite / .sqlite3 (and anything else) default to SQLite.
        return cls(SQLiteBackend(path), safety)

    # ------------------------------------------------------------------
    # Untrusted path (agent-reachable)
    # ------------------------------------------------------------------
    def execute(
        self,
        sql: str,
        *,
        validate: bool = True,
        timeout_s: float | None = None,
        max_rows: int | None = None,
    ) -> QueryResult:
        """Execute one sandboxed statement under a wall-clock timeout."""
        if self._poisoned:
            raise QueryTimeoutError(
                "engine was abandoned after a previous timeout (DuckDB connection closed)"
            )
        if validate:
            result = validate_or_raise(sql, self.backend.dialect)  # type: ignore[arg-type]
            sql = result.sql

        limit = (max_rows if max_rows is not None else self.safety.max_rows_returned) + 1
        timeout = timeout_s if timeout_s is not None else self.safety.query_timeout_seconds

        columns, raw_rows, elapsed_ms = self._run_fetch(sql, timeout, limit)
        effective_max = limit - 1
        truncated = len(raw_rows) > effective_max
        rows = [[_to_jsonable(v) for v in row] for row in raw_rows[:effective_max]]
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=round(elapsed_ms, 3),
        )

    # ------------------------------------------------------------------
    # Trusted introspection / profiling path (module-generated SQL only)
    # ------------------------------------------------------------------
    def introspect(self) -> CatalogSnapshot:
        """Snapshot tables, columns and approximate row counts."""
        if self.backend.dialect == "sqlite":
            return self._introspect_sqlite()
        return self._introspect_duckdb()

    def profile_column(self, table: str, column: str, top_k: int | None = None) -> ColumnProfile:
        """Compute null/distinct statistics and top-k values for one column."""
        k = top_k if top_k is not None else self.safety.max_column_sample_values
        catalog = self.introspect()
        table_info = next((t for t in catalog.tables if t.name == table), None)
        if table_info is None:
            raise ValueError(f"unknown table: {table!r}")
        if not any(c.name == column for c in table_info.columns):
            raise ValueError(f"unknown column {column!r} on table {table!r}")

        t, c = _quote_identifier(table), _quote_identifier(column)
        counts = self._execute_privileged(
            f"SELECT COUNT(*), COUNT({c}), COUNT(DISTINCT {c}) FROM {t}"
        )
        total, non_null, distinct = counts.rows[0][0], counts.rows[0][1], counts.rows[0][2]

        top = self._execute_privileged(
            f"SELECT {c} AS value, COUNT(*) AS freq FROM {t} "
            f"GROUP BY {c} ORDER BY freq DESC, value ASC LIMIT {int(k)}"
        )
        top_values = [
            ValueCount(value="NULL" if row[0] is None else str(row[0]), count=int(row[1]))
            for row in top.rows
        ]

        numeric_stats: NumericStats | None = None
        try:
            stats = self._execute_privileged(f"SELECT MIN({c}), MAX({c}), AVG({c}) FROM {t}")
            minimum, maximum, mean = stats.rows[0]
            numeric_stats = NumericStats(
                minimum=None if minimum is None else float(minimum),
                maximum=None if maximum is None else float(maximum),
                mean=None if mean is None else float(mean),
            )
        except (QueryExecutionError, ValueError, TypeError):
            numeric_stats = None  # non-numeric column; statistics simply omitted

        total_i = int(total)
        return ColumnProfile(
            table=table,
            column=column,
            total_rows=total_i,
            null_count=total_i - int(non_null),
            null_ratio=round((total_i - int(non_null)) / total_i, 4) if total_i else 0.0,
            distinct_count=int(distinct) if distinct is not None else None,
            top_values=top_values,
            numeric_stats=numeric_stats,
        )

    def explain(self, sql: str) -> QueryResult:
        """Return the driver's query plan for an already-validated statement."""
        validated = validate_or_raise(sql, self.backend.dialect)  # type: ignore[arg-type]
        prefix = "EXPLAIN QUERY PLAN" if self.backend.dialect == "sqlite" else "EXPLAIN"
        return self._execute_privileged(f"{prefix} {validated.sql}")

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.backend.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _run_fetch(
        self, sql: str, timeout: float, limit: int
    ) -> tuple[list[str], list[tuple[Any, ...]], float]:
        start = perf_counter()
        future: Future[tuple[list[str], list[tuple[Any, ...]]]] = self._executor.submit(
            self.backend.fetch, sql, limit
        )
        try:
            columns, raw_rows = future.result(timeout=timeout)
        except FuturesTimeoutError as exc:
            self.backend.cancel()
            if self.backend.driver_name == "duckdb":
                self._poisoned = True
            raise QueryTimeoutError(f"statement exceeded the {timeout}s execution budget") from exc
        return columns, raw_rows, (perf_counter() - start) * 1000.0

    def _execute_privileged(self, sql: str) -> QueryResult:
        """Run module-generated trusted SQL with the same timeout discipline."""
        if self._poisoned:
            raise QueryTimeoutError("engine is unavailable after a previous timeout")
        columns, raw_rows, elapsed_ms = self._run_fetch(
            sql, self.safety.query_timeout_seconds, self.safety.max_rows_returned
        )
        rows = [[_to_jsonable(v) for v in row] for row in raw_rows]
        return QueryResult(
            columns=columns, rows=rows, row_count=len(rows), elapsed_ms=round(elapsed_ms, 3)
        )

    def _introspect_sqlite(self) -> CatalogSnapshot:
        snapshot = CatalogSnapshot(dialect="sqlite")
        names = self._execute_privileged(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        for row in names.rows:
            table = str(row[0])
            quoted = _quote_identifier(table)
            columns_result = self._execute_privileged(f"PRAGMA table_info({quoted})")
            columns = [
                ColumnInfo(name=str(c[1]), type=str(c[2] or "?"), nullable=not int(c[3] or 0))
                for c in columns_result.rows
            ]
            try:
                count = self._execute_privileged(f"SELECT COUNT(*) FROM {quoted}")
                row_count: int | None = int(count.rows[0][0])
            except (QueryExecutionError, ValueError, IndexError):
                row_count = None
            snapshot.tables.append(TableInfo(name=table, columns=columns, row_count=row_count))
        return snapshot

    def _introspect_duckdb(self) -> CatalogSnapshot:
        snapshot = CatalogSnapshot(dialect="duckdb")
        names = self._execute_privileged(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        )
        for row in names.rows:
            table = str(row[0])
            quoted = _quote_identifier(table)
            columns_result = self._execute_privileged(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                f"WHERE table_schema = 'main' AND table_name = '{table}' "
                "ORDER BY ordinal_position"
            )
            columns = [
                ColumnInfo(name=str(c[0]), type=str(c[1]), nullable=str(c[2]).upper() == "YES")
                for c in columns_result.rows
            ]
            try:
                count = self._execute_privileged(f"SELECT COUNT(*) FROM {quoted}")
                row_count = int(count.rows[0][0])
            except (QueryExecutionError, ValueError, IndexError):
                row_count = None
            snapshot.tables.append(TableInfo(name=table, columns=columns, row_count=row_count))
        return snapshot
