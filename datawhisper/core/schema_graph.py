"""Graph-based schema pruning for large catalogs.

Why a graph instead of raw DDL
------------------------------
Enterprise schemas routinely exceed what fits in a prompt, and dumping every
``CREATE TABLE`` both burns tokens and degrades accuracy (the model latches on
to irrelevant tables). This module keeps a compact entity/relationship graph
and selects only the sub-schema needed for a question:

1. **Retrieve** tables with BM25 over identifiers (table and column names),
   which is cheap, deterministic and needs no embedding service.
2. **Connect** the selected tables with shortest join paths found by Dijkstra
   over foreign-key edges, so every table handed to the model is reachable.
3. **Compress** wide tables to the columns that matter (query-relevant names
   plus primary/foreign keys), then render within a token budget.

Foreign keys come from the driver catalog when declared. Because many real
databases omit constraints, the builder also infers edges from the ubiquitous
``<entity>_id`` convention, but only when the referenced table and its primary
key actually exist - inference never fabricates a target.
"""

from __future__ import annotations

import heapq
import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, Field

from datawhisper.core.config import SchemaGraphConfig
from datawhisper.core.models import CatalogSnapshot, ForeignKeyInfo, TableInfo

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "this",
        "to",
        "was",
        "were",
        "what",
        "which",
        "who",
        "with",
        "show",
        "list",
        "me",
        "give",
        "all",
        "per",
        "each",
    }
)


def tokenize(text: str) -> list[str]:
    """Split identifiers and prose into lowercase unigrams.

    ``customerId``, ``customer_id`` and ``customer id`` all yield the same
    tokens, which is what makes BM25 viable across naming conventions.
    """
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return [
        token
        for token in re.findall(r"[a-z0-9]+", spaced.lower())
        if len(token) > 1 and token not in _STOPWORDS
    ]


# ---------------------------------------------------------------------------
# Graph models
# ---------------------------------------------------------------------------
class GraphColumn(BaseModel):
    """A pruned column with its relevance score and key role."""

    name: str
    type: str = "UNKNOWN"
    nullable: bool = True
    primary_key: bool = False
    foreign_key: bool = False
    score: float = 0.0


class GraphTable(BaseModel):
    """A selected table after column pruning."""

    name: str
    row_count: int | None = None
    columns: list[GraphColumn] = Field(default_factory=list)
    score: float = 0.0
    is_view: bool = False


class JoinStep(BaseModel):
    """One equijoin edge: ``left_table.left_column = right_table.right_column``."""

    left_table: str
    left_column: str
    right_table: str
    right_column: str

    def sql_condition(self) -> str:
        return f"{self.left_table}.{self.left_column} = {self.right_table}.{self.right_column}"

    def reversed(self) -> JoinStep:
        return JoinStep(
            left_table=self.right_table,
            left_column=self.right_column,
            right_table=self.left_table,
            right_column=self.left_column,
        )


class JoinPlan(BaseModel):
    """A connected subgraph expressed as join steps in traversal order."""

    tables: list[str] = Field(default_factory=list)
    steps: list[JoinStep] = Field(default_factory=list)
    hops: int = 0
    unreachable: list[str] = Field(default_factory=list)


class PrunedSchema(BaseModel):
    """The sub-schema handed to the model, plus provenance for diagnostics."""

    tables: list[GraphTable] = Field(default_factory=list)
    join_plan: JoinPlan | None = None
    dropped_tables: list[str] = Field(default_factory=list)
    query_tokens: list[str] = Field(default_factory=list)

    @property
    def table_names(self) -> list[str]:
        return [table.name for table in self.tables]

    def render(self, max_tokens: int = 1500) -> str:
        """Render a prompt-ready description within an approximate token budget.

        Four characters per token is the standard rough estimate for English
        identifier-heavy text and is deliberately conservative: over-pruning a
        column is recoverable (the agent can call ``inspect_schema``), while
        overflowing the prompt is not.
        """
        budget_chars = max(200, max_tokens * 4)
        tables = list(self.tables)
        plan = self.join_plan

        while True:
            text = self._render_text(tables, plan)
            if len(text) <= budget_chars or len(tables) <= 1:
                break
            # Drop the least relevant table that is not required by a join step.
            steps = plan.steps if plan is not None else []
            protected = {step.left_table for step in steps} | {step.right_table for step in steps}
            droppable = [t for t in tables if t.name not in protected] or tables
            victim = min(droppable, key=lambda t: (t.score, t.name))
            tables = [t for t in tables if t.name != victim.name]
            if plan is not None:
                kept = {t.name for t in tables}
                steps = [
                    step
                    for step in plan.steps
                    if step.left_table in kept and step.right_table in kept
                ]
                plan = JoinPlan(tables=[t.name for t in tables], steps=steps, hops=len(steps))

        text = self._render_text(tables, plan)
        if len(text) > budget_chars:
            text = text[: budget_chars - 20].rstrip() + "\n... (truncated)"
        return text

    @staticmethod
    def _render_text(tables: Sequence[GraphTable], plan: JoinPlan | None) -> str:
        lines: list[str] = []
        for table in tables:
            rows = "?" if table.row_count is None else str(table.row_count)
            view = " view" if table.is_view else ""
            lines.append(f"Table {table.name} ({rows} rows{view}) score={table.score:.2f}")
            rendered = []
            for column in table.columns:
                marks = []
                if column.primary_key:
                    marks.append("PK")
                if column.foreign_key:
                    marks.append("FK")
                suffix = f" [{'/'.join(marks)}]" if marks else ""
                rendered.append(f"{column.name}:{column.type}{suffix}")
            if rendered:
                lines.append("  " + ", ".join(rendered))
        if plan is not None and plan.steps:
            lines.append("Join paths:")
            lines.extend(f"  {step.sql_condition()}" for step in plan.steps)
        if plan is not None and plan.unreachable:
            lines.append("Unconnected tables (no FK path): " + ", ".join(plan.unreachable))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------
class SchemaGraph:
    """FK adjacency graph with BM25 retrieval and shortest-path joining."""

    def __init__(
        self,
        catalog: CatalogSnapshot,
        config: SchemaGraphConfig | None = None,
    ) -> None:
        self.catalog = catalog
        self.config = config or SchemaGraphConfig()
        self._tables: dict[str, TableInfo] = {t.name.lower(): t for t in catalog.tables}
        self._adjacency: dict[str, dict[str, JoinStep]] = {}
        self._keys: list[ForeignKeyInfo] = []
        self._outgoing_fk_columns: dict[str, set[str]] = {}
        self._docs: dict[str, list[str]] = {}
        self._idf: dict[str, float] = {}
        self._avgdl: float = 0.0
        self._build()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build(self) -> None:
        for name in self._tables:
            self._adjacency[name] = {}
        self._keys = self._collect_foreign_keys()
        for key in self._keys:
            self._outgoing_fk_columns.setdefault(key.from_table.lower(), set()).update(
                key.from_columns
            )
        for key in self._keys:
            if len(key.from_columns) != 1 or len(key.to_columns) != 1:
                continue  # composite keys are rendered but not traversed
            step = JoinStep(
                left_table=key.from_table.lower(),
                left_column=key.from_columns[0],
                right_table=key.to_table.lower(),
                right_column=key.to_columns[0],
            )
            self._adjacency.setdefault(step.left_table, {}).setdefault(step.right_table, step)
            self._adjacency.setdefault(step.right_table, {}).setdefault(
                step.left_table, step.reversed()
            )

        self._docs = {
            name: tokenize(name) + [token for c in table.columns for token in tokenize(c.name)]
            for name, table in self._tables.items()
        }
        document_frequency: Counter[str] = Counter()
        for tokens in self._docs.values():
            document_frequency.update(set(tokens))
        total_docs = max(1, len(self._docs))
        self._idf = {
            token: math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
            for token, df in document_frequency.items()
        }
        self._avgdl = (
            sum(len(tokens) for tokens in self._docs.values()) / total_docs if self._docs else 0.0
        )

    def _collect_foreign_keys(self) -> list[ForeignKeyInfo]:
        keys: list[ForeignKeyInfo] = []
        declared: set[tuple[str, str, str, str]] = set()
        for table in self._tables.values():
            for key in table.foreign_keys:
                if self._tables.get(key.to_table.lower()) is None:
                    continue
                keys.append(key)
                for source, target in zip(key.from_columns, key.to_columns, strict=False):
                    declared.add(
                        (table.name.lower(), source.lower(), key.to_table.lower(), target.lower())
                    )

        for name, table in self._tables.items():
            for column in table.columns:
                lowered = column.name.lower()
                if not (lowered.endswith("_id") or lowered.endswith("_uuid")):
                    continue
                base = lowered[:-3] if lowered.endswith("_id") else lowered[:-5]
                target_table = self._resolve_target(base)
                if target_table is None or target_table.name.lower() == name:
                    continue
                target_pk = target_table.primary_key or [
                    c.name for c in target_table.columns if c.name.lower() in {"id", "uuid"}
                ]
                if not target_pk:
                    continue
                target_column = target_pk[0]
                signature = (name, lowered, target_table.name.lower(), target_column.lower())
                if signature in declared:
                    continue
                declared.add(signature)
                keys.append(
                    ForeignKeyInfo(
                        from_table=table.name,
                        from_columns=[column.name],
                        to_table=target_table.name,
                        to_columns=[target_column],
                    )
                )
        return keys

    def _resolve_target(self, base: str) -> TableInfo | None:
        """Resolve an entity stem to a table using common pluralization."""
        candidates = {base, f"{base}s", f"{base}es"}
        if base.endswith("ies"):
            candidates.add(f"{base[:-3]}y")
        if base.endswith("y"):
            candidates.add(f"{base[:-1]}ies")
        for candidate in candidates:
            table = self._tables.get(candidate)
            if table is not None:
                return table
        return None

    # ------------------------------------------------------------------
    # Scoring and retrieval
    # ------------------------------------------------------------------
    def bm25(self, query: str) -> dict[str, float]:
        """Score every table against the question with Okapi BM25."""
        tokens = tokenize(query)
        if not tokens:
            return dict.fromkeys(self._tables, 0.0)
        scores: dict[str, float] = {}
        k1, b = self.config.bm25_k1, self.config.bm25_b
        for name, doc_tokens in self._docs.items():
            counts = Counter(doc_tokens)
            length = len(doc_tokens) or 1
            score = 0.0
            for token in tokens:
                frequency = counts.get(token, 0)
                if not frequency:
                    continue
                idf = self._idf.get(token, 0.0)
                denominator = frequency + k1 * (1 - b + b * length / (self._avgdl or 1.0))
                score += idf * (frequency * (k1 + 1)) / denominator
            # A direct hit on the table name outranks an incidental column hit.
            if set(tokens) & set(tokenize(name)):
                score *= 1.5
            scores[name] = score
        return scores

    def retrieve(self, query: str, top_k: int | None = None) -> PrunedSchema:
        """Select, connect and compress the sub-schema relevant to ``query``."""
        limit = top_k if top_k is not None else self.config.max_tables
        scores = self.bm25(query)
        tokens = tokenize(query)

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        positive = [name for name, score in ranked if score > 0]
        if positive:
            seeds = positive[:limit]
        else:
            # No lexical overlap: prefer well-connected hubs, which are the
            # most likely entry points for exploratory questions.
            seeds = sorted(
                self._tables, key=lambda name: (-len(self._adjacency.get(name, {})), name)
            )[:limit]

        plan = self.connect(seeds)
        allowed = limit + self.config.include_neighbor_tables
        selected = list(seeds)
        for name in plan.tables:
            if name not in selected and len(selected) < allowed:
                selected.append(name)
        # Keep only tables that survived the budget cap, and rebuild the path.
        final_plan = (
            self.connect([name for name in selected if name in plan.tables])
            if len(selected) > 1
            else JoinPlan(tables=selected)
        )
        plan = final_plan

        tables = [
            self._prune_table(name, tokens, scores.get(name, 0.0))
            for name in selected
            if name in self._tables
        ]
        tables.sort(key=lambda t: (-t.score, t.name))
        dropped = [name for name in self._tables if name not in selected]
        return PrunedSchema(
            tables=tables,
            join_plan=plan,
            dropped_tables=sorted(dropped),
            query_tokens=tokens,
        )

    def _prune_table(self, name: str, tokens: Sequence[str], score: float) -> GraphTable:
        table = self._tables[name]
        # Only columns that actually hold a reference are foreign keys. The
        # adjacency graph lists both directions, so using it here would label
        # the parent-side primary key as an FK.
        fk_columns = set(self._outgoing_fk_columns.get(name, set()))
        token_set = set(tokens)

        columns: list[GraphColumn] = []
        seen: set[str] = set()
        for column in table.columns:
            column_tokens = set(tokenize(column.name))
            relevance = sum(self._idf.get(token, 0.0) for token in column_tokens & token_set)
            is_fk = column.name in fk_columns
            if column.primary_key or is_fk or relevance > 0:
                columns.append(
                    GraphColumn(
                        name=column.name,
                        type=column.type,
                        nullable=column.nullable,
                        primary_key=column.primary_key,
                        foreign_key=is_fk,
                        score=relevance,
                    )
                )
                seen.add(column.name)

        # Always give the model enough context to understand a row, even when
        # the question matched only a key column.
        if len(columns) < 3:
            for column in table.columns:
                if column.name in seen:
                    continue
                columns.append(
                    GraphColumn(
                        name=column.name,
                        type=column.type,
                        nullable=column.nullable,
                        primary_key=column.primary_key,
                        foreign_key=column.name in fk_columns,
                    )
                )
                seen.add(column.name)
                if len(columns) >= 6:
                    break

        columns.sort(key=lambda c: (not c.primary_key, not c.foreign_key, -c.score, c.name))
        return GraphTable(
            name=table.name,
            row_count=table.row_count,
            columns=columns[: self.config.max_columns_per_table],
            score=score,
            is_view=table.is_view,
        )

    # ------------------------------------------------------------------
    # Pathfinding
    # ------------------------------------------------------------------
    def neighbors(self, table: str) -> dict[str, JoinStep]:
        return self._adjacency.get(table.lower(), {})

    def shortest_path(self, source: str, target: str) -> JoinPlan | None:
        """Dijkstra over FK edges (unit weight) returning the join chain."""
        start, goal = source.lower(), target.lower()
        if start not in self._tables or goal not in self._tables:
            return None
        if start == goal:
            return JoinPlan(tables=[start], steps=[], hops=0)

        distances: dict[str, int] = {start: 0}
        previous: dict[str, tuple[str, JoinStep]] = {}
        counter = 0
        queue: list[tuple[int, int, str]] = [(0, counter, start)]
        visited: set[str] = set()

        while queue:
            distance, _, node = heapq.heappop(queue)
            if node in visited:
                continue
            visited.add(node)
            if node == goal:
                break
            for neighbour, step in self._adjacency.get(node, {}).items():
                candidate = distance + 1
                if candidate < distances.get(neighbour, math.inf):
                    distances[neighbour] = candidate
                    previous[neighbour] = (node, step)
                    counter += 1
                    heapq.heappush(queue, (candidate, counter, neighbour))

        if goal not in distances:
            return None
        steps: list[JoinStep] = []
        order: list[str] = [goal]
        cursor = goal
        while cursor != start:
            parent, step = previous[cursor]
            steps.append(step)
            order.append(parent)
            cursor = parent
        steps.reverse()
        order.reverse()
        return JoinPlan(tables=order, steps=steps, hops=len(steps))

    def connect(self, tables: Sequence[str]) -> JoinPlan:
        """Connect a set of tables with a minimum-ish spanning tree of FK paths.

        A Prim-style expansion is used: start from the first table and always
        attach the closest unconnected table via multi-source Dijkstra. The
        result is deterministic and, for the unit-weight FK graph, optimal in
        total hops for the selected order.
        """
        requested = [t.lower() for t in tables if t.lower() in self._tables]
        if not requested:
            return JoinPlan()
        connected: list[str] = [requested[0]]
        pending: set[str] = set(requested[1:])
        steps: list[JoinStep] = []
        unreachable: list[str] = []

        while pending:
            path = self._multi_source_path(connected, pending)
            if path is None:
                unreachable = sorted(pending)
                break
            for node in path.tables:
                if node not in connected:
                    connected.append(node)
            steps.extend(path.steps)
            for node in path.tables:
                pending.discard(node)

        return JoinPlan(
            tables=connected,
            steps=steps,
            hops=len(steps),
            unreachable=unreachable,
        )

    def _multi_source_path(self, sources: Iterable[str], targets: set[str]) -> JoinPlan | None:
        """Dijkstra from many sources; returns the closest path to any target."""
        distances: dict[str, int] = dict.fromkeys(sources, 0)
        previous: dict[str, tuple[str, JoinStep]] = {}
        counter = 0
        queue: list[tuple[int, int, str]] = []
        for source in sources:
            queue.append((0, counter, source))
            counter += 1
        heapq.heapify(queue)
        visited: set[str] = set()
        goal: str | None = None

        while queue:
            distance, _, node = heapq.heappop(queue)
            if node in visited:
                continue
            visited.add(node)
            if node in targets:
                goal = node
                break
            for neighbour, step in self._adjacency.get(node, {}).items():
                candidate = distance + 1
                if candidate < distances.get(neighbour, math.inf):
                    distances[neighbour] = candidate
                    previous[neighbour] = (node, step)
                    counter += 1
                    heapq.heappush(queue, (candidate, counter, neighbour))

        if goal is None:
            return None
        steps: list[JoinStep] = []
        order: list[str] = [goal]
        cursor = goal
        while cursor in previous:
            parent, step = previous[cursor]
            steps.append(step)
            order.append(parent)
            cursor = parent
        steps.reverse()
        order.reverse()
        return JoinPlan(tables=order, steps=steps, hops=len(steps))
