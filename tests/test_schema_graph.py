"""Schema graph: BM25 retrieval, column pruning, token budget, pathfinding."""

from __future__ import annotations

from datawhisper.core.config import SchemaGraphConfig
from datawhisper.core.models import CatalogSnapshot, ColumnInfo, TableInfo
from datawhisper.core.schema_graph import SchemaGraph, tokenize


def _graph(catalog: CatalogSnapshot, **overrides: object) -> SchemaGraph:
    config = SchemaGraphConfig(**overrides)  # type: ignore[arg-type]
    return SchemaGraph(catalog, config)


# ---------------------------------------------------------------------------
# Tokenization and scoring
# ---------------------------------------------------------------------------
def test_tokenize_handles_naming_conventions() -> None:
    assert tokenize("customerId") == ["customer", "id"]
    assert tokenize("customer_id") == ["customer", "id"]
    assert tokenize("Customer ID") == ["customer", "id"]


def test_bm25_prefers_table_name_hits(large_catalog: CatalogSnapshot) -> None:
    graph = _graph(large_catalog)
    scores = graph.bm25("rows in t40 with metric value")
    assert scores["t40"] > 0
    assert max(scores, key=lambda name: scores[name]) == "t40"


def test_retrieval_respects_top_k(large_catalog: CatalogSnapshot) -> None:
    graph = _graph(large_catalog, max_tables=6, include_neighbor_tables=2)
    pruned = graph.retrieve("what is the metric value in t40", top_k=6)
    assert "t40" in pruned.table_names
    assert len(pruned.tables) <= 8
    assert len(pruned.dropped_tables) >= 50


def test_retrieval_falls_back_to_hubs_without_overlap(large_catalog: CatalogSnapshot) -> None:
    graph = _graph(large_catalog, max_tables=4)
    pruned = graph.retrieve("qqq zzz", top_k=4)
    assert pruned.tables, "fallback must still return candidate tables"
    assert len(pruned.tables) <= 6


# ---------------------------------------------------------------------------
# Join pathfinding
# ---------------------------------------------------------------------------
def test_shortest_path_across_chain(large_catalog: CatalogSnapshot) -> None:
    graph = _graph(large_catalog)
    plan = graph.shortest_path("t5", "t40")
    assert plan is not None
    assert plan.hops == 35
    assert plan.tables[0] == "t5" and plan.tables[-1] == "t40"
    assert all(step.left_table != step.right_table for step in plan.steps)


def test_shortest_path_is_symmetric_and_zero_length(large_catalog: CatalogSnapshot) -> None:
    graph = _graph(large_catalog)
    forward = graph.shortest_path("t10", "t20")
    backward = graph.shortest_path("t20", "t10")
    assert forward is not None and backward is not None
    assert forward.hops == backward.hops == 10
    assert graph.shortest_path("t3", "t3").hops == 0  # type: ignore[union-attr]


def test_shortest_path_unknown_table_returns_none(large_catalog: CatalogSnapshot) -> None:
    graph = _graph(large_catalog)
    assert graph.shortest_path("t1", "does_not_exist") is None


def test_connect_builds_spanning_tree(large_catalog: CatalogSnapshot) -> None:
    graph = _graph(large_catalog)
    plan = graph.connect(["t0", "t10", "t20"])
    assert plan.tables[0] == "t0"
    assert {"t10", "t20"}.issubset(set(plan.tables))
    assert plan.hops == 20
    assert plan.steps


def test_disconnected_tables_are_reported() -> None:
    catalog = CatalogSnapshot(
        dialect="sqlite",
        tables=[
            TableInfo(
                name="left",
                columns=[ColumnInfo(name="id", type="INTEGER", primary_key=True)],
            ),
            TableInfo(
                name="right",
                columns=[ColumnInfo(name="id", type="INTEGER", primary_key=True)],
            ),
        ],
    )
    graph = _graph(catalog)
    plan = graph.connect(["left", "right"])
    assert plan.unreachable == ["right"]


# ---------------------------------------------------------------------------
# FK inference and column pruning
# ---------------------------------------------------------------------------
def test_heuristic_fk_inference() -> None:
    catalog = CatalogSnapshot(
        dialect="sqlite",
        tables=[
            TableInfo(
                name="customers",
                columns=[
                    ColumnInfo(name="id", type="INTEGER", primary_key=True),
                    ColumnInfo(name="name", type="TEXT"),
                ],
            ),
            TableInfo(
                name="orders",
                columns=[
                    ColumnInfo(name="id", type="INTEGER", primary_key=True),
                    ColumnInfo(name="customer_id", type="INTEGER"),
                    ColumnInfo(name="amount", type="REAL"),
                ],
            ),
        ],
    )
    graph = _graph(catalog)
    plan = graph.shortest_path("orders", "customers")
    assert plan is not None and plan.hops == 1
    assert plan.steps[0].left_column == "customer_id"
    assert plan.steps[0].right_column == "id"


def test_column_pruning_keeps_keys_and_relevant_columns() -> None:
    catalog = CatalogSnapshot(
        dialect="sqlite",
        tables=[
            TableInfo(
                name="wide_events",
                columns=[
                    ColumnInfo(name="id", type="INTEGER", primary_key=True),
                    ColumnInfo(name="user_id", type="INTEGER"),
                    ColumnInfo(name="revenue_amount", type="REAL"),
                    *[ColumnInfo(name=f"filler_{index}", type="TEXT") for index in range(30)],
                ],
            ),
            TableInfo(
                name="users",
                columns=[
                    ColumnInfo(name="id", type="INTEGER", primary_key=True),
                    ColumnInfo(name="name", type="TEXT"),
                ],
            ),
        ],
    )
    graph = _graph(catalog, max_columns_per_table=6)
    pruned = graph.retrieve("total revenue amount per user", top_k=2)
    wide = next(table for table in pruned.tables if table.name == "wide_events")
    names = [column.name for column in wide.columns]
    assert "id" in names
    assert "revenue_amount" in names
    assert "user_id" in names
    assert len(names) <= 6


def test_render_stays_within_token_budget(large_catalog: CatalogSnapshot) -> None:
    graph = _graph(large_catalog, max_tables=10, max_context_tokens=200)
    pruned = graph.retrieve("metric value in t30", top_k=10)
    rendered = pruned.render(max_tokens=200)
    assert len(rendered) // 4 <= 210
    assert "Table t30" in rendered


def test_render_includes_join_paths(large_catalog: CatalogSnapshot) -> None:
    graph = _graph(large_catalog, max_tables=4)
    pruned = graph.retrieve("t10 metric and t12 label", top_k=4)
    rendered = pruned.render(800)
    assert "Join paths:" in rendered
    assert "=" in rendered
