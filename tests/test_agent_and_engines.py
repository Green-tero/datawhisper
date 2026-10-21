"""Multi-engine execution and the cost/error self-correction loop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from datawhisper.core.agent import DataWhisperAgent, ScriptedProvider
from datawhisper.core.config import CostPolicy, DataWhisperConfig, SafetyThresholds
from datawhisper.core.database import (
    DuckDBBackend,
    ExecutionEngine,
    QueryExecutionError,
    SQLiteBackend,
)
from datawhisper.core.models import CatalogSnapshot, ColumnInfo, PlanInfo, TableInfo
from datawhisper.core.security import CostViolation, SQLSecurityError


def _turn(action: str, **payload: object) -> str:
    return json.dumps({"thought": "step", "action": action, "action_input": payload})


def _final(answer: str, sql: str | None = None) -> str:
    return json.dumps(
        {
            "thought": "done",
            "action": "final_answer",
            "action_input": {"answer": answer, "sql": sql},
        }
    )


def _agent(engine: ExecutionEngine, responses: list[str]) -> DataWhisperAgent:
    config = DataWhisperConfig(
        safety=SafetyThresholds(query_timeout_seconds=5.0, max_sql_repair_attempts=3),
        cost=CostPolicy(max_scan_rows=5, auto_limit=5),
    )
    return DataWhisperAgent(engine, ScriptedProvider(responses), config)


# ---------------------------------------------------------------------------
# SQLite execution and bounds
# ---------------------------------------------------------------------------
def test_sqlite_query_returns_scrubbed_rows(engine: ExecutionEngine) -> None:
    result = engine.execute("SELECT status, COUNT(*) AS n FROM orders GROUP BY status")
    assert result.row_count == 3
    assert result.columns == ["status", "n"]
    assert result.cost_action in {"allow", "limit"}


def test_row_cap_marks_truncation(engine: ExecutionEngine) -> None:
    result = engine.execute("SELECT id FROM orders", max_rows=3)
    assert result.row_count == 3
    assert result.truncated is True


def test_cost_gate_injects_limit(sqlite_db_path: Path, config: DataWhisperConfig) -> None:
    gated_config = DataWhisperConfig(
        safety=config.safety, cost=CostPolicy(max_scan_rows=5, auto_limit=5)
    )
    gated = ExecutionEngine.from_source(sqlite_db_path, gated_config)
    try:
        result = gated.execute("SELECT * FROM orders")
        assert result.cost_action == "limit"
        assert result.limit_applied == 5
        assert result.row_count <= 5
    finally:
        gated.close()


def test_cost_gate_rejects_cartesian(engine: ExecutionEngine) -> None:
    with pytest.raises(CostViolation) as excinfo:
        engine.execute("SELECT * FROM customers, orders")
    assert excinfo.value.decision.cartesian_groups


def test_explain_only_does_not_execute(engine: ExecutionEngine) -> None:
    plan, decision = engine.explain("SELECT * FROM orders")
    assert plan.dialect == "sqlite"
    assert decision.allowed
    assert any(node.node_type in {"SCAN", "INDEX_SCAN"} for node in plan.nodes)


def test_engine_blocks_ast_violations(engine: ExecutionEngine) -> None:
    with pytest.raises(SQLSecurityError):
        engine.execute("DELETE FROM orders")


def test_in_memory_backend_smoke() -> None:
    backend = SQLiteBackend(None)
    engine = ExecutionEngine(backend)
    try:
        backend.connection.execute("CREATE TABLE t (x INTEGER)")
        backend.connection.execute("INSERT INTO t VALUES (1), (2), (3)")
        backend.connection.commit()
        assert engine.execute("SELECT SUM(x) FROM t").rows[0][0] == 6
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# Columnar engines: Parquet, JSONL, native DuckDB
# ---------------------------------------------------------------------------
def test_parquet_via_duckdb(parquet_path: Path, config: DataWhisperConfig) -> None:
    pytest.importorskip("duckdb")
    engine = ExecutionEngine.from_source(parquet_path, config)
    try:
        catalog = engine.introspect()
        assert catalog.dialect == "duckdb"
        assert catalog.tables[0].name == "analytics"
        result = engine.execute('SELECT COUNT(*) AS n FROM "analytics"')
        assert result.rows[0][0] == 4
    finally:
        engine.close()


def test_jsonl_vectorized_extraction(jsonl_path: Path, config: DataWhisperConfig) -> None:
    pytest.importorskip("duckdb")
    engine = ExecutionEngine.from_source(jsonl_path, config)
    try:
        result = engine.execute('SELECT id, kind FROM "events" ORDER BY id')
        assert [row[1] for row in result.rows] == ["login", "click", "login"]
        nested = engine.execute('SELECT user.email FROM "events" LIMIT 1')
        flattened = str(nested.rows)
        assert "ada@example.com" not in flattened
        assert nested.redactions is not None
    finally:
        engine.close()


def test_native_duckdb_file(tmp_path: Path, config: DataWhisperConfig) -> None:
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "warehouse.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE sales (id INTEGER, region TEXT, amount DOUBLE)")
    connection.execute("INSERT INTO sales VALUES (1, 'eu', 10.0), (2, 'us', 20.0)")
    connection.close()

    engine = ExecutionEngine.from_source(path, config)
    try:
        assert engine.introspect().tables[0].name == "sales"
        assert engine.execute("SELECT SUM(amount) FROM sales").rows[0][0] == 30.0
    finally:
        engine.close()


def test_arrow_transfer_is_native(parquet_path: Path, config: DataWhisperConfig) -> None:
    pytest.importorskip("duckdb")
    pa = pytest.importorskip("pyarrow")
    backend = DuckDBBackend.from_parquet(parquet_path)
    try:
        result = backend.fetch('SELECT * FROM "analytics"', 10)
        table = result.to_arrow()
        assert isinstance(table, pa.Table)
        assert table.num_rows == 4
        assert result.num_rows == 4
    finally:
        backend.close()


def test_duckdb_rejects_user_file_functions(parquet_path: Path) -> None:
    """The AST sandbox, not DuckDB config, is the guard for file access."""
    pytest.importorskip("duckdb")
    from datawhisper.core.security import SQLSecurityError, validate_or_raise

    backend = DuckDBBackend.from_parquet(parquet_path)
    try:
        with pytest.raises(SQLSecurityError):
            validate_or_raise("SELECT * FROM read_csv('/etc/passwd')", "duckdb")
        # Only the engine-registered view is visible to the agent.
        assert [table.name for table in backend.introspect().tables] == ["analytics"]
    finally:
        backend.close()


def test_directory_of_parquet(
    tmp_path: Path, parquet_path: Path, config: DataWhisperConfig
) -> None:
    pytest.importorskip("duckdb")
    engine = ExecutionEngine.from_source(parquet_path.parent, config)
    try:
        assert engine.introspect().tables
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# Self-correction
# ---------------------------------------------------------------------------
def test_repairs_missing_column(engine: ExecutionEngine) -> None:
    good = "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"
    agent = _agent(
        engine,
        [
            _turn("run_sql", sql="SELECT bogus FROM orders"),
            _turn("run_sql", sql=good),
            _final("Three statuses; paid dominates.", good),
        ],
    )
    result = agent.run("break down orders by status")
    assert result.success
    assert result.steps[0].observation.startswith("[SQL ERROR]")
    assert "TOOL ERROR" not in result.steps[1].observation
    assert len(result.steps) == 2


def test_repairs_after_security_rejection(engine: ExecutionEngine) -> None:
    agent = _agent(
        engine,
        [
            _turn("run_sql", sql="DROP TABLE orders"),
            _turn("run_sql", sql="SELECT COUNT(*) AS n FROM orders"),
            _final("Twelve orders."),
        ],
    )
    result = agent.run("how many orders?")
    assert result.success
    assert result.steps[0].observation.startswith("[SECURITY ERROR]")


def test_repairs_after_cost_rejection(engine: ExecutionEngine) -> None:
    agent = _agent(
        engine,
        [
            _turn("run_sql", sql="SELECT * FROM customers, orders"),
            _turn("run_sql", sql="SELECT COUNT(*) AS n FROM orders"),
            _final("Twelve orders."),
        ],
    )
    result = agent.run("count everything")
    assert result.success
    assert result.steps[0].observation.startswith("[COST ERROR]")
    assert "cartesian" in result.steps[0].observation.lower()


def test_repair_budget_exhaustion(engine: ExecutionEngine) -> None:
    failing = _turn("run_sql", sql="SELECT nope FROM orders")
    agent = _agent(engine, [failing] * 6)
    result = agent.run("impossible")
    assert not result.success
    assert len(result.steps) <= 4
    assert "failed attempts" in result.answer
    assert result.telemetry


def test_agent_never_sees_raw_pii(engine: ExecutionEngine) -> None:
    agent = _agent(
        engine,
        [
            _turn("run_sql", sql="SELECT email, card_number FROM customers"),
            _final("Customer contact data is redacted; 3 rows exist."),
        ],
    )
    result = agent.run("customer contacts")
    observation = result.steps[0].observation
    assert "ada@example.com" not in observation
    assert "4111" not in observation
    assert "EMAIL" in observation
    assert result.redactions is not None and result.redactions.total >= 3


def test_agent_uses_pruned_context(engine: ExecutionEngine) -> None:
    agent = _agent(engine, [_final("Nothing to compute.")])
    result = agent.run("how many orders per status?")
    assert result.pruned_tables
    assert "orders" in result.pruned_tables
    assert result.join_hops >= 0


def test_unknown_action_is_recoverable(engine: ExecutionEngine) -> None:
    agent = _agent(engine, [_turn("launch_missiles"), _final("No.")])
    result = agent.run("bad tool")
    assert result.steps[0].observation.startswith("[TOOL ERROR]")
    assert result.success


def test_fenced_json_turns_are_parsed(engine: ExecutionEngine) -> None:
    raw = f"Here is the plan:\n```json\n{_final('ok', 'SELECT 1')}\n```"
    action, payload = DataWhisperAgent._parse_message(raw)
    assert action == "final_answer" and payload["answer"] == "ok"


class _LeakyBackend:
    """Backend that echoes a SQL literal in its driver error."""

    dialect = "sqlite"
    driver_name = "sqlite"

    def fetch(self, sql: str, limit: int) -> object:
        raise QueryExecutionError("syntax error near 'ada@example.com'", driver="sqlite")

    def explain(self, sql: str) -> PlanInfo:
        return PlanInfo(dialect="sqlite", nodes=[])

    def introspect(self) -> CatalogSnapshot:
        return CatalogSnapshot(
            dialect="sqlite",
            tables=[TableInfo(name="t", columns=[ColumnInfo(name="id")])],
        )

    def cancel(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_driver_errors_are_scrubbed_before_reaching_the_agent() -> None:
    engine = ExecutionEngine(_LeakyBackend(), DataWhisperConfig(cost=CostPolicy(enabled=False)))
    try:
        with pytest.raises(QueryExecutionError) as excinfo:
            engine.execute("SELECT 1")
        message = str(excinfo.value)
        assert "ada@example.com" not in message
        assert "email_" in message
    finally:
        engine.close()
