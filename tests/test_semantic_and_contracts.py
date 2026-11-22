"""Tests for Semantic Layer & Data Contracts governance."""

from __future__ import annotations

from datawhisper.core.database import ExecutionEngine
from datawhisper.core.semantic import (
    ContractRule,
    ContractRuleType,
    DimensionDefinition,
    MetricDefinition,
    SemanticCatalog,
    SemanticModel,
    TableContract,
    verify_table_contract,
)


def test_semantic_catalog_and_metric_resolution() -> None:
    catalog = SemanticCatalog(
        models=[
            SemanticModel(
                name="sales",
                table="orders",
                primary_key=["id"],
                dimensions=[
                    DimensionDefinition(name="status", expression="status", dim_type="categorical")
                ],
                metrics=[
                    MetricDefinition(
                        name="total_revenue",
                        expression="SUM(amount)",
                        table="orders",
                        filter="status = 'COMPLETED'",
                    ),
                    MetricDefinition(
                        name="average_order_value",
                        expression="AVG(amount)",
                        table="orders",
                    ),
                ],
            )
        ],
        standalone_metrics=[
            MetricDefinition(
                name="active_customers",
                expression="COUNT(DISTINCT customer_id)",
                table="orders",
            )
        ],
    )

    assert catalog.get_metric("total_revenue") is not None
    assert catalog.get_metric("non_existent") is None

    # Exact and token-based resolution
    resolved = catalog.resolve_metric_for_query("What is our total revenue this month?")
    assert resolved is not None
    assert resolved.name == "total_revenue"
    assert "WHERE status = 'COMPLETED'" in resolved.sql_snippet()

    resolved_standalone = catalog.resolve_metric_for_query("Count active customers")
    assert resolved_standalone is not None
    assert resolved_standalone.name == "active_customers"


def test_data_contract_verification_passing(engine: ExecutionEngine) -> None:
    # Contract on customers table: checks that id is unique and country is within allowed values
    contract = TableContract(
        table="customers",
        rules=[
            ContractRule(column="id", rule_type=ContractRuleType.NOT_NULL),
            ContractRule(column="id", rule_type=ContractRuleType.UNIQUE),
            ContractRule(
                column="country",
                rule_type=ContractRuleType.ALLOWED_VALUES,
                allowed_values=["US", "UK", "IR"],
            ),
            ContractRule(rule_type=ContractRuleType.ROW_COUNT, min_rows=1, max_rows=100),
        ],
    )

    report = verify_table_contract(engine, contract)
    assert report.passed
    assert report.checked_rules == 4
    assert len(report.violations) == 0


def test_data_contract_verification_detects_violations(engine: ExecutionEngine) -> None:
    # Orders table has NULL amounts in conftest fixtures
    contract = TableContract(
        table="orders",
        rules=[
            ContractRule(column="amount", rule_type=ContractRuleType.NOT_NULL),
            ContractRule(
                column="status",
                rule_type=ContractRuleType.ALLOWED_VALUES,
                allowed_values=["COMPLETED"],  # fixture has other statuses
            ),
            ContractRule(rule_type=ContractRuleType.ROW_COUNT, min_rows=1000),  # violates min_rows
        ],
    )

    report = verify_table_contract(engine, contract)
    assert not report.passed
    assert len(report.violations) == 3

    # Check specific violations
    rule_types = {v.rule_type for v in report.violations}
    assert "not_null" in rule_types
    assert "allowed_values" in rule_types
    assert "row_count" in rule_types


def test_data_contract_escaping_and_range_rule(engine: ExecutionEngine) -> None:
    # Test single-quote escaping in allowed values and range rule
    contract = TableContract(
        table="customers",
        rules=[
            ContractRule(
                column="country",
                rule_type=ContractRuleType.ALLOWED_VALUES,
                allowed_values=["O'Brien", "D'Souza", "US", "UK", "IR"],
            ),
            ContractRule(
                column="id",
                rule_type=ContractRuleType.RANGE,
                min_value=1,
                max_value=1000,
            ),
        ],
    )
    report = verify_table_contract(engine, contract)
    assert report.passed
    assert report.checked_rules == 2
