"""Example script demonstrating declarative Data Contracts and Semantic Catalog in DataWhisper."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from datawhisper.core.database import ExecutionEngine, SQLiteBackend
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


def main() -> None:
    # 1. Setup temporary database
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "store.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                country TEXT NOT NULL,
                status TEXT NOT NULL
            );
            INSERT INTO customers VALUES
                (1, 'Ada Lovelace', 'ada@example.com', 'UK', 'active'),
                (2, 'Alan Turing', 'alan@example.com', 'UK', 'active'),
                (3, 'Grace Hopper', 'grace@example.org', 'US', 'pending');
            """
        )
        conn.commit()
        conn.close()

        backend = SQLiteBackend(db_path)
        with ExecutionEngine(backend) as engine:
            # 2. Define a Table Contract
            contract = TableContract(
                table="customers",
                rules=[
                    ContractRule(
                        rule_type=ContractRuleType.NOT_NULL,
                        column="email",
                    ),
                    ContractRule(
                        rule_type=ContractRuleType.ALLOWED_VALUES,
                        column="status",
                        allowed_values=["active", "pending", "suspended"],
                    ),
                    ContractRule(
                        rule_type=ContractRuleType.ROW_COUNT,
                        min_rows=2,
                    ),
                ],
            )

            print("--- Verifying Table Contract ---")
            report = verify_table_contract(engine, contract)
            print(f"Contract for '{contract.table}': {'PASSED' if report.passed else 'FAILED'}")
            print(f"Rules checked: {report.checked_rules}, Violations: {len(report.violations)}")
            for v in report.violations:
                print(f"  [FAIL] {v.rule_type}: {v.message}")

            # 3. Define Semantic Catalog with Metrics
            catalog = SemanticCatalog(
                models=[
                    SemanticModel(
                        name="customer_model",
                        table="customers",
                        primary_key=["id"],
                        dimensions=[
                            DimensionDefinition(
                                name="country", expression="country", dim_type="categorical"
                            ),
                            DimensionDefinition(
                                name="status", expression="status", dim_type="categorical"
                            ),
                        ],
                        metrics=[
                            MetricDefinition(
                                name="total_customers",
                                expression="COUNT(*)",
                                table="customers",
                            ),
                        ],
                    )
                ]
            )

            print("\n--- Semantic Catalog Summary ---")
            print(f"Models: {[m.name for m in catalog.models]}")
            print(f"Metrics: {[m.name for m in catalog.models[0].metrics]}")


if __name__ == "__main__":
    main()
