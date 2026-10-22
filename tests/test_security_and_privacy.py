"""AST sandbox, cost gatekeeper and PII redaction: attack and leak tests."""

from __future__ import annotations

import pytest
from datawhisper.core.config import CostPolicy, PrivacyConfig
from datawhisper.core.models import PlanInfo, PlanNode
from datawhisper.core.privacy import (
    PiiCategory,
    PrivacyScrubber,
    column_category,
    contains_pii,
    detect_pii,
    luhn_valid,
)
from datawhisper.core.security import (
    CostGatekeeper,
    SQLSecurityError,
    apply_limit,
    current_limit,
    detect_cartesian_joins,
    transpile_sql,
    validate_or_raise,
    validate_sql,
)

# ---------------------------------------------------------------------------
# AST sandbox
# ---------------------------------------------------------------------------
ATTACKS = [
    ("DELETE FROM orders", "sqlite"),
    ("DROP TABLE orders", "sqlite"),
    ("UPDATE orders SET amount = 0", "sqlite"),
    ("INSERT INTO orders VALUES (1)", "sqlite"),
    ("CREATE TABLE pwn (x INTEGER)", "sqlite"),
    ("ALTER TABLE orders ADD COLUMN z INTEGER", "sqlite"),
    ("PRAGMA table_info(orders)", "sqlite"),
    ("ATTACH DATABASE 'evil.db' AS evil", "sqlite"),
    ("VACUUM", "sqlite"),
    ("INSTALL httpfs", "duckdb"),
    ("COPY orders TO 'exfil.csv'", "duckdb"),
    ("SELECT 1; DROP TABLE orders", "sqlite"),
    ("SELECT 1 --; DROP TABLE orders", "sqlite"),
    ("SELECT /* inline */ * FROM orders", "sqlite"),
    ("SELECT * FROM read_csv('secret.csv')", "duckdb"),
    ("SELECT * FROM read_parquet('/etc/passwd')", "duckdb"),
    ("SELECT load_extension('evil')", "sqlite"),
    ("SELECT getenv('GITHUB_TOKEN')", "duckdb"),
    ("SELECT * FROM duckdb_secrets()", "duckdb"),
    ("SELECT pg_read_file('/etc/passwd')", "postgres"),
    ("SELECT pg_sleep(30)", "postgres"),
    ("SELECT * FROM secret_db.main.orders", "duckdb"),
    ("WITH t AS (SELECT 1 AS x) DELETE FROM orders", "sqlite"),
]

VALID = [
    ("SELECT 1", "sqlite"),
    ("SELECT COUNT(*) FROM orders", "sqlite"),
    ("WITH t AS (SELECT 1 AS x) SELECT * FROM t", "sqlite"),
    ("SELECT status FROM orders UNION SELECT 'x' AS status", "sqlite"),
    ("SELECT * FROM main.orders", "sqlite"),
    ("SELECT * FROM public.orders", "postgres"),
    ("SELECT * FROM information_schema.tables", "duckdb"),
    ("SELECT list_transform([1, 2], x -> x + 1)", "duckdb"),
    ("SELECT 1;", "sqlite"),
]


@pytest.mark.parametrize(("sql", "dialect"), ATTACKS)
def test_attack_vectors_are_rejected(sql: str, dialect: str) -> None:
    result = validate_sql(sql, dialect)
    assert not result.ok, f"allowed: {sql}"
    assert result.violations


@pytest.mark.parametrize(("sql", "dialect"), VALID)
def test_legitimate_queries_pass(sql: str, dialect: str) -> None:
    result = validate_sql(sql, dialect)
    assert result.ok, f"blocked: {sql} -> {[v.code for v in result.violations]}"


def test_unparseable_sql_fails_closed() -> None:
    result = validate_sql("SELECT FROM WHERE", "sqlite")
    assert not result.ok
    assert result.violations[0].code in {"PARSE_ERROR", "MULTI_STATEMENT"}


def test_validate_or_raise_exposes_violations() -> None:
    with pytest.raises(SQLSecurityError) as excinfo:
        validate_or_raise("DROP TABLE orders", "sqlite")
    assert any(
        v.code in {"BLOCKED_STATEMENT", "NON_READ_ONLY_ROOT"} for v in excinfo.value.violations
    )


# ---------------------------------------------------------------------------
# Dialect transpilation
# ---------------------------------------------------------------------------
def test_transpile_roundtrip_is_valid_in_every_target() -> None:
    sql = "SELECT customer_id, SUM(amount) AS total FROM orders GROUP BY customer_id"
    for target in ("duckdb", "postgres"):
        output = transpile_sql(sql, "sqlite", target)
        assert validate_sql(output, target).ok, output
    assert transpile_sql("SELECT 1 LIMIT 5", "duckdb", "postgres").upper().endswith("LIMIT 5")


# ---------------------------------------------------------------------------
# LIMIT helpers and cartesian detection
# ---------------------------------------------------------------------------
def test_limit_helpers() -> None:
    assert current_limit("SELECT 1", "sqlite") is None
    assert current_limit("SELECT 1 LIMIT 10", "sqlite") == 10
    rewritten = apply_limit("SELECT * FROM orders", "sqlite", 25)
    assert rewritten is not None and "LIMIT 25" in rewritten
    # Never widen an existing, tighter limit.
    assert apply_limit("SELECT * FROM orders LIMIT 5", "sqlite", 25) == (
        "SELECT * FROM orders LIMIT 5"
    )


def test_cartesian_detection_flags_implicit_cross_join() -> None:
    groups = detect_cartesian_joins("SELECT * FROM customers, orders", "sqlite")
    assert groups and len(groups[0]) == 2


def test_cartesian_detection_accepts_real_joins() -> None:
    assert (
        detect_cartesian_joins(
            "SELECT * FROM customers c JOIN orders o ON c.id = o.customer_id", "sqlite"
        )
        == []
    )
    assert (
        detect_cartesian_joins(
            "SELECT * FROM customers c, orders o WHERE c.id = o.customer_id", "sqlite"
        )
        == []
    )
    assert (
        detect_cartesian_joins(
            "SELECT * FROM customers c CROSS JOIN orders o WHERE c.id = o.customer_id", "sqlite"
        )
        == []
    )


def test_cartesian_detection_handles_subquery_isolation() -> None:
    sql = "SELECT * FROM (SELECT id FROM orders) AS o " "JOIN customers c ON c.id = o.id"
    assert detect_cartesian_joins(sql, "sqlite") == []


# ---------------------------------------------------------------------------
# Cost gatekeeper
# ---------------------------------------------------------------------------
def _full_scan_plan(rows: float, tables: list[str] | None = None) -> PlanInfo:
    names = tables or ["orders"]
    return PlanInfo(
        dialect="sqlite",
        nodes=[PlanNode(node_type="SCAN", table=name, estimated_rows=rows) for name in names],
    )


def test_gate_injects_limit_on_large_unbounded_scan() -> None:
    gate = CostGatekeeper(CostPolicy(max_scan_rows=1000, auto_limit=50))
    decision = gate.evaluate("SELECT * FROM orders", _full_scan_plan(1_000_000))
    assert decision.allowed and decision.action == "limit"
    assert decision.rewritten_sql is not None and "LIMIT 50" in decision.rewritten_sql


def test_gate_allows_already_bounded_scan() -> None:
    gate = CostGatekeeper(CostPolicy(max_scan_rows=1000, auto_limit=50))
    decision = gate.evaluate("SELECT * FROM orders LIMIT 10", _full_scan_plan(1_000_000))
    assert decision.allowed and decision.action == "allow"
    assert any("already bounded" in reason for reason in decision.reasons)


def test_gate_rejects_cartesian_product() -> None:
    gate = CostGatekeeper(CostPolicy(reject_cartesian_joins=True))
    decision = gate.evaluate(
        "SELECT * FROM customers, orders", _full_scan_plan(10, ["customers", "orders"])
    )
    assert not decision.allowed and decision.action == "reject"
    assert decision.cartesian_groups


def test_gate_rejects_cross_join_operator() -> None:
    gate = CostGatekeeper(CostPolicy())
    plan = PlanInfo(dialect="duckdb", nodes=[PlanNode(node_type="CROSS_PRODUCT")])
    assert not gate.evaluate("SELECT 1", plan).allowed


def test_gate_uses_catalog_rows_when_planner_omits_estimates(engine: object) -> None:
    gate = CostGatekeeper(CostPolicy(max_scan_rows=5, auto_limit=3))
    decision = gate.evaluate(
        "SELECT * FROM orders", PlanInfo(dialect="sqlite", nodes=[]), {"orders": 12}
    )
    assert decision.allowed and decision.action == "limit"


def test_plan_failure_policy_can_fail_closed() -> None:
    plan = PlanInfo(dialect="sqlite", error="planner exploded")
    permissive = CostGatekeeper(CostPolicy()).evaluate("SELECT 1", plan)
    strict = CostGatekeeper(CostPolicy(plan_failure_policy="reject")).evaluate("SELECT 1", plan)
    assert permissive.allowed
    assert not strict.allowed


# ---------------------------------------------------------------------------
# PII detectors
# ---------------------------------------------------------------------------
def test_luhn_validation() -> None:
    assert luhn_valid("4111111111111111")
    assert not luhn_valid("4111111111111112")
    assert not luhn_valid("1234")


@pytest.mark.parametrize(
    ("value", "category"),
    [
        ("write to ada@example.com please", PiiCategory.EMAIL),
        ("card 4111 1111 1111 1111 on file", PiiCategory.CREDIT_CARD),
        ("ssn 123-45-6789", PiiCategory.SSN),
        ("call +1 555-123-4567 now", PiiCategory.PHONE),
        ("key sk-live-abcdefghijklmnopqrstuvwx", PiiCategory.API_KEY),
        ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789", PiiCategory.API_KEY),
        ("aws AKIAIOSFODNN7EXAMPLE", PiiCategory.API_KEY),
        (
            "auth eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
            PiiCategory.JWT,
        ),
    ],
)
def test_value_detectors(value: str, category: PiiCategory) -> None:
    matches = detect_pii(value)
    assert any(match.category is category for match in matches), matches


def test_invalid_card_number_is_not_flagged_as_card() -> None:
    categories = {match.category for match in detect_pii("id 4111111111111112")}
    assert PiiCategory.CREDIT_CARD not in categories


def test_column_name_inference() -> None:
    assert column_category("user_email") is PiiCategory.EMAIL
    assert column_category("credit_card_number") is PiiCategory.CREDIT_CARD
    assert column_category("api_key") is PiiCategory.API_KEY
    assert column_category("total_amount") is None
    assert contains_pii("phone")


# ---------------------------------------------------------------------------
# Scrubber modes and zero-leak invariants
# ---------------------------------------------------------------------------
def test_hash_mode_never_leaks_raw_values() -> None:
    scrubber = PrivacyScrubber(PrivacyConfig(mode="hash", salt="s"))
    rows = [["ada@example.com", "4111 1111 1111 1111", "ok"]]
    scrubbed, report = scrubber.scrub(["email", "card_number", "note"], rows)
    flattened = " ".join(str(cell) for row in scrubbed for cell in row)
    assert "ada@example.com" not in flattened
    assert "4111" not in flattened
    assert report.total == 2
    assert report.by_category == {"EMAIL": 1, "CREDIT_CARD": 1}
    assert "ada@example.com" not in report.model_dump_json()


def test_hash_mode_is_deterministic_for_same_salt() -> None:
    first, _ = PrivacyScrubber(PrivacyConfig(mode="hash", salt="stable")).scrub(
        ["email"], [["a@x.com"]]
    )
    second, _ = PrivacyScrubber(PrivacyConfig(mode="hash", salt="stable")).scrub(
        ["email"], [["a@x.com"]]
    )
    assert first == second


def test_tokenize_mode_is_stable_within_run() -> None:
    scrubber = PrivacyScrubber(PrivacyConfig(mode="tokenize"))
    scrubbed, report = scrubber.scrub(["email"], [["a@x.com"], ["a@x.com"], ["b@x.com"]])
    assert scrubbed[0] == scrubbed[1]
    assert scrubbed[0] != scrubbed[2]
    assert scrubber.scrub_text("a@x.com")[0] == "<EMAIL:1>"
    assert scrubber.scrub_text("b@x.com")[0] == "<EMAIL:2>"
    assert report.by_category["EMAIL"] == 3


def test_mask_mode_keeps_last_four_only() -> None:
    scrubber = PrivacyScrubber(PrivacyConfig(mode="mask"))
    scrubbed, _ = scrubber.scrub(
        ["card_number", "email", "ssn"],
        [["4111 1111 1111 1111", "ada@example.com", "123-45-6789"]],
    )
    assert scrubbed[0][0] == "**** **** **** 1111"
    assert scrubbed[0][1] == "a***@example.com"
    assert scrubbed[0][2] == "***-**-6789"


def test_redact_mode_is_explicit() -> None:
    scrubber = PrivacyScrubber(PrivacyConfig(mode="redact"))
    scrubbed, _ = scrubber.scrub(["email"], [["a@x.com"]])
    assert scrubbed == [["[REDACTED:EMAIL]"]]


def test_free_text_scrubbing_preserves_context() -> None:
    scrubber = PrivacyScrubber(PrivacyConfig(mode="redact"))
    text, report = scrubber.scrub_text("login by ada@example.com from London")
    assert "ada@example.com" not in text
    assert text.startswith("login by [REDACTED:EMAIL]")
    assert "London" in text
    assert report.total == 1


def test_allowlist_bypasses_column_inference() -> None:
    scrubber = PrivacyScrubber(PrivacyConfig(column_allowlist=("email",)))
    scrubbed, report = scrubber.scrub(["email"], [["a@x.com"]])
    assert scrubbed == [["a@x.com"]]
    assert report.total == 0


# ---------------------------------------------------------------------------
# End-to-end: engine output is already scrubbed
# ---------------------------------------------------------------------------
def test_engine_scrubs_query_results(engine: object) -> None:
    result = engine.execute("SELECT email, card_number, ssn FROM customers")  # type: ignore[attr-defined]
    flattened = " ".join(str(cell) for row in result.rows for cell in row)
    assert "ada@example.com" not in flattened
    assert "4111" not in flattened
    assert "123-45-6789" not in flattened
    assert result.redactions is not None
    assert result.redactions.by_category["EMAIL"] == 3


def test_engine_scrubs_profile_top_values(engine: object) -> None:
    profile = engine.profile_column("customers", "email")
    values = " ".join(entry.value for entry in profile.top_values)
    assert "ada@example.com" not in values
    assert profile.redactions is not None and profile.redactions.total > 0


def test_engine_does_not_touch_safe_columns(engine: object) -> None:
    result = engine.execute("SELECT status, COUNT(*) AS n FROM orders GROUP BY status")
    assert result.redactions is None
    assert result.rows


def test_engine_blocks_mutation(engine: object) -> None:
    # Destructive DDL/DML is stopped by the System-1 firewall before the AST
    # even parses; both layers raise, so accept either as the enforcement.
    from datawhisper.core.security import SecurityViolation as FirewallViolation

    with pytest.raises((SQLSecurityError, FirewallViolation)):
        engine.execute("DROP TABLE orders")  # type: ignore[attr-defined]
    assert any(t.name == "orders" for t in engine.introspect().tables)  # type: ignore[attr-defined]


def test_nested_limit_does_not_bound_the_outer_query() -> None:
    sql = "SELECT * FROM (SELECT * FROM orders LIMIT 5) AS x"
    assert current_limit(sql, "sqlite") is None
    gate = CostGatekeeper(CostPolicy(max_scan_rows=10, auto_limit=7))
    decision = gate.evaluate(sql, _full_scan_plan(1000))
    assert decision.action == "limit"
    assert decision.rewritten_sql is not None
    assert decision.rewritten_sql.rstrip().endswith("LIMIT 7")


def test_using_and_natural_joins_are_not_cartesian() -> None:
    assert detect_cartesian_joins("SELECT * FROM a JOIN b USING (id)", "sqlite") == []
    assert detect_cartesian_joins("SELECT * FROM a NATURAL JOIN b", "sqlite") == []


def test_gate_uses_ast_tables_when_plan_has_no_estimates() -> None:
    gate = CostGatekeeper(CostPolicy(max_scan_rows=5, auto_limit=3))
    plan = PlanInfo(dialect="sqlite", nodes=[PlanNode(node_type="SCAN", table=None)])
    decision = gate.evaluate("SELECT * FROM orders", plan, {"orders": 12})
    assert decision.action == "limit"
