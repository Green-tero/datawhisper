"""ReAct loop, self-correction, sandbox integration and engine edge cases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from datawhisper.core.agent import (
    AgentParseError,
    DataWhisperAgent,
    ProviderError,
    ScriptedProvider,
    build_provider,
)
from datawhisper.core.config import DataWhisperConfig, LLMConfig, SafetyThresholds
from datawhisper.core.database import ExecutionEngine, QueryTimeoutError, SQLiteBackend


def _turn(action: str, **action_input: object) -> str:
    return json.dumps({"thought": "step", "action": action, "action_input": action_input})


def _final(answer: str, sql: str | None = None) -> str:
    return json.dumps(
        {
            "thought": "done",
            "action": "final_answer",
            "action_input": {"answer": answer, "sql": sql},
        }
    )


def _agent(engine: ExecutionEngine, responses: list[str]) -> DataWhisperAgent:
    config = DataWhisperConfig(safety=SafetyThresholds(query_timeout_seconds=5.0))
    return DataWhisperAgent(engine, ScriptedProvider(responses), config)


def test_self_correction_heals_broken_sql(engine: ExecutionEngine) -> None:
    """The core promise: a driver error on turn 1 is repaired on turn 2."""
    bad_sql = "SELECT bogus_column FROM orders"
    good_sql = "SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY n DESC"
    agent = _agent(
        engine,
        [
            _turn("run_sql", sql=bad_sql),
            _turn("run_sql", sql=good_sql),
            _final("paid dominates the order book (6 of 12).", good_sql),
        ],
    )
    result = agent.run("Break down orders by status.")

    assert result.success is True
    assert result.iterations == 2
    assert "TOOL ERROR" in result.steps[0].observation
    assert "no such column" in result.steps[0].observation.lower()
    assert "TOOL ERROR" not in result.steps[1].observation
    assert result.sql == good_sql
    assert "paid" in result.answer


def test_repair_budget_exhaustion_fails_gracefully(engine: ExecutionEngine) -> None:
    """Four consecutive broken statements must abort, not loop forever."""
    failing = _turn("run_sql", sql="SELECT also_bogus FROM orders")
    agent = _agent(engine, [failing] * 5)
    config = agent._config.safety
    result = agent.run("run something impossible")
    assert result.success is False
    assert len(result.steps) <= config.max_sql_repair_attempts + 1
    assert "failed SQL attempts" in result.answer
    assert result.telemetry


def test_sandbox_blocks_destructive_tool_call(engine: ExecutionEngine) -> None:
    """Even a hostile/hallucinating model cannot reach a mutating statement."""
    agent = _agent(
        engine,
        [
            _turn("run_sql", sql="DROP TABLE orders"),
            _final("Could not mutate the database."),
        ],
    )
    result = agent.run("delete everything")
    observation = result.steps[0].observation
    assert observation.startswith("[TOOL ERROR]")
    assert "sandbox" in observation
    # And the table is still there.
    assert any(t.name == "orders" for t in engine.introspect().tables)


def test_format_error_recovery_with_feedback(engine: ExecutionEngine) -> None:
    agent = _agent(engine, ["the answer is obviously 42", _final("12 orders total.")])
    result = agent.run("how many orders?")
    assert result.success is True
    assert result.iterations == 0  # format retries do not consume tool steps
    assert "12 orders" in result.answer


def test_unknown_action_returns_tool_error(engine: ExecutionEngine) -> None:
    agent = _agent(
        engine,
        [_turn("launch_missiles"), _final("No missiles here.")],
    )
    result = agent.run("do something invalid")
    assert result.steps[0].observation.startswith("[TOOL ERROR]")
    assert "unknown action" in result.steps[0].observation
    assert result.success is True


def test_fenced_json_and_prose_are_parsed(engine: ExecutionEngine) -> None:
    raw = f"Sure! Here is my plan:\n```json\n{_final('ok', 'SELECT 1')}\n```"
    action, action_input = DataWhisperAgent._parse_message(raw)
    assert action == "final_answer"
    assert action_input["answer"] == "ok"


def test_parse_message_rejects_non_json() -> None:
    with pytest.raises(AgentParseError):
        DataWhisperAgent._parse_message("no braces at all")


# ---------------------------------------------------------------------------
# Provider behavior
# ---------------------------------------------------------------------------
def test_scripted_provider_exhaustion_raises() -> None:
    provider = ScriptedProvider(["one"])
    assert provider.complete([{"role": "user", "content": "hi"}]) == "one"
    with pytest.raises(ProviderError):
        provider.complete([{"role": "user", "content": "again"}])


def test_build_provider_without_configuration_raises() -> None:
    with pytest.raises(ProviderError):
        build_provider(LLMConfig(provider="none"))


def test_build_provider_missing_key_raises() -> None:
    with pytest.raises(ProviderError):
        build_provider(LLMConfig(provider="openai", api_key=""))


# ---------------------------------------------------------------------------
# Execution-engine edge cases exercised through the same fixtures
# ---------------------------------------------------------------------------
def test_row_cap_truncation_flag(engine: ExecutionEngine) -> None:
    result = engine.execute("SELECT id FROM orders", max_rows=5)
    assert result.row_count == 5
    assert result.truncated is True


def test_empty_table_query(engine: ExecutionEngine) -> None:
    result = engine.execute("SELECT COUNT(*) AS n FROM empty_metrics")
    assert result.rows[0][0] == 0
    profile = engine.profile_column("empty_metrics", "gauge")
    assert profile.total_rows == 0
    assert profile.null_ratio == 0.0
    assert profile.top_values == []


def test_null_heavy_column_profile(engine: ExecutionEngine) -> None:
    profile = engine.profile_column("orders", "status")
    assert profile.total_rows == 12
    assert profile.distinct_count == 3
    assert profile.top_values[0].value == "paid"
    assert profile.top_values[0].count == 6
    assert profile.null_ratio == 0.0

    amounts = engine.profile_column("orders", "amount")
    assert amounts.null_count == 2
    assert amounts.null_ratio == pytest.approx(2 / 12, abs=1e-3)
    assert amounts.numeric_stats is not None
    assert amounts.numeric_stats.minimum == pytest.approx(15.0)


def test_timeout_interrupts_infinite_recursion(engine: ExecutionEngine) -> None:
    infinite = (
        "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM cnt) "
        "SELECT COUNT(*) FROM cnt"
    )
    with pytest.raises(QueryTimeoutError):
        engine.execute(infinite, timeout_s=0.4)


def test_csv_ingestion_roundtrip(tmp_path: Path) -> None:
    csv_path = tmp_path / "lead scores.csv"
    csv_path.write_text(
        "lead id,score,stage\n1,88.5,MQL\n2,,disqualified\n3,92.1,SQL\n",
        encoding="utf-8",
    )
    engine = ExecutionEngine.from_source(csv_path)
    try:
        snapshot = engine.introspect()
        assert len(snapshot.tables) == 1
        table = snapshot.tables[0]
        assert table.columns[0].name == "lead_id"
        result = engine.execute('SELECT COUNT(*) AS n FROM "lead_scores" WHERE score IS NOT NULL')
        assert result.rows[0][0] == 2
    finally:
        engine.close()


def test_in_memory_engine_smoke() -> None:
    engine = ExecutionEngine(SQLiteBackend(None))
    try:
        engine.backend.connection.execute("CREATE TABLE t (x INTEGER)")
        engine.backend.connection.execute("INSERT INTO t VALUES (1), (2), (3)")
        engine.backend.connection.commit()
        assert engine.execute("SELECT SUM(x) AS s FROM t").rows[0][0] == 6
    finally:
        engine.close()
