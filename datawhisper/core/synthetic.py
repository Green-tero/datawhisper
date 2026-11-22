"""Synthetic Mock Data Generator for air-gapped CI benchmarks and offline agent testing.

Generates structurally coherent, referentially integral synthetic datasets directly
from a CatalogSnapshot, populating in-memory databases without touching production data.
"""

from __future__ import annotations

import random
from typing import Any

from pydantic import BaseModel, Field

from datawhisper.core.config import DataWhisperConfig
from datawhisper.core.database import ExecutionEngine, SQLiteBackend
from datawhisper.core.models import CatalogSnapshot, TableInfo


class SyntheticConfig(BaseModel):
    """Configuration for synthetic data generation."""

    rows_per_table: int = Field(default=20, ge=1, le=10000)
    seed: int = 42


def _quote_ident(name: str) -> str:
    """Safely quote a SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def _topological_sort_tables(tables: list[TableInfo]) -> list[TableInfo]:
    """Sort tables topologically according to foreign key dependencies using Kahn's algorithm."""
    table_map = {t.name.lower(): t for t in tables}
    deps: dict[str, set[str]] = {t.name.lower(): set() for t in tables}
    dependents: dict[str, set[str]] = {t.name.lower(): set() for t in tables}

    for t in tables:
        t_name = t.name.lower()
        for fk in t.foreign_keys:
            target = fk.to_table.lower()
            if target in table_map and target != t_name:
                deps[t_name].add(target)
                dependents[target].add(t_name)

    queue = [name for name, d in deps.items() if len(d) == 0]
    sorted_names: list[str] = []

    while queue:
        node = queue.pop(0)
        sorted_names.append(node)
        for child in list(dependents.get(node, ())):
            deps[child].discard(node)
            if len(deps[child]) == 0:
                queue.append(child)

    # If cyclic dependencies exist, append remaining tables deterministically
    for t in tables:
        t_name = t.name.lower()
        if t_name not in sorted_names:
            sorted_names.append(t_name)

    return [table_map[n] for n in sorted_names]


class SyntheticDataGenerator:
    """Deterministic generator producing referentially valid mock tables from catalog schemas."""

    def __init__(self, config: SyntheticConfig | None = None) -> None:
        self.config = config or SyntheticConfig()

    def generate_table_rows(
        self,
        table: TableInfo,
        generated_pks: dict[str, list[Any]],
        rng: random.Random,
    ) -> list[list[Any]]:
        """Generate N synthetic rows for a single table respecting types and FK relationships."""
        rows: list[list[Any]] = []
        n_rows = self.config.rows_per_table

        # Map FK columns to referenced table PK list
        fk_map: dict[str, list[Any]] = {}
        for fk in table.foreign_keys:
            target = fk.to_table.lower()
            if generated_pks.get(target):
                for src_col in fk.from_columns:
                    fk_map[src_col.lower()] = generated_pks[target]

        t_name = table.name.lower()
        for i in range(1, n_rows + 1):
            row: list[Any] = []
            for col in table.columns:
                c_name = col.name.lower()
                c_type = (col.type or "TEXT").upper()

                # 1. Primary key
                if col.primary_key:
                    if "INT" in c_type or c_type in {"REAL", "NUMERIC"}:
                        val: Any = i
                    else:
                        val = f"{t_name}_{i}"
                    row.append(val)
                    continue

                # 2. Foreign key
                if c_name in fk_map:
                    val = rng.choice(fk_map[c_name])
                    row.append(val)
                    continue

                # 3. Typed mock value
                if "INT" in c_type:
                    val = (i * 10) + rng.randint(0, 9)
                elif any(k in c_type for k in ("FLOAT", "REAL", "DOUBLE", "NUMERIC", "DECIMAL")):
                    val = round((i * 15.5) + rng.random(), 2)
                elif "BOOL" in c_type:
                    val = bool(i % 2 == 0)
                elif "DATE" in c_type:
                    day = (i % 28) + 1
                    val = f"2026-01-{day:02d}"
                elif "TIME" in c_type:
                    day = (i % 28) + 1
                    val = f"2026-01-{day:02d} 10:00:00"
                else:
                    if "email" in c_name:
                        val = f"user_{t_name}_{i}@example.mock"
                    elif "name" in c_name:
                        val = f"{t_name.capitalize()} {i}"
                    elif "status" in c_name:
                        val = ["active", "pending", "completed"][i % 3]
                    elif "country" in c_name:
                        val = ["US", "UK", "DE", "FR"][i % 4]
                    else:
                        val = f"{c_name}_{i}"
                row.append(val)
            rows.append(row)

        return rows

    def generate_dataset(self, catalog: CatalogSnapshot) -> dict[str, list[list[Any]]]:
        """Generate a complete synthetic dataset topologically ordered by foreign keys."""
        rng = random.Random(self.config.seed)
        generated_pks: dict[str, list[Any]] = {}
        dataset: dict[str, list[list[Any]]] = {}

        # Sort tables: parent tables (referenced by FKs) generated before child tables
        sorted_tables = _topological_sort_tables(list(catalog.tables))

        for table in sorted_tables:
            t_name = table.name.lower()
            rows = self.generate_table_rows(table, generated_pks, rng)
            dataset[t_name] = rows

            # Extract generated PKs for downstream foreign key references
            pk_indices = [idx for idx, col in enumerate(table.columns) if col.primary_key]
            if pk_indices:
                pk_idx = pk_indices[0]
                generated_pks[t_name] = [r[pk_idx] for r in rows]
            else:
                # Fallback to first column
                generated_pks[t_name] = [r[0] for r in rows]

        return dataset

    def generate_in_memory_engine(
        self,
        catalog: CatalogSnapshot,
        config: DataWhisperConfig | None = None,
    ) -> ExecutionEngine:
        """Create an in-memory SQLite database populated with the synthetic dataset."""
        dataset = self.generate_dataset(catalog)
        backend = SQLiteBackend(None)

        # Create tables in topological order respecting foreign keys
        conn = backend.connection
        for table in _topological_sort_tables(list(catalog.tables)):
            col_defs: list[str] = []
            pk_cols = [col.name for col in table.columns if col.primary_key]
            for col in table.columns:
                pk_mark = " PRIMARY KEY" if col.primary_key and len(pk_cols) == 1 else ""
                col_defs.append(f"{_quote_ident(col.name)} {col.type}{pk_mark}")
            if len(pk_cols) > 1:
                pk_clause = f"PRIMARY KEY ({', '.join(_quote_ident(c) for c in pk_cols)})"
                col_defs.append(pk_clause)

            q_table = _quote_ident(table.name)
            conn.execute(f"CREATE TABLE {q_table} ({', '.join(col_defs)})")

            # Insert rows
            t_name = table.name.lower()
            rows = dataset.get(t_name, [])
            if rows:
                placeholders = ", ".join("?" for _ in table.columns)
                conn.executemany(
                    f"INSERT INTO {q_table} VALUES ({placeholders})",
                    rows,
                )
        conn.commit()
        return ExecutionEngine(backend, config or DataWhisperConfig())
