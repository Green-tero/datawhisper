"""Zero-framework ReAct agent with graph retrieval, cost awareness and self-healing.

Pipeline
--------
1. **Sub-schema retrieval** - BM25 ranks tables, Dijkstra connects them, wide
   tables are compressed to relevant columns.
2. **SQL generation** - the model writes dialect-specific SQL (transpilation is
   available when a dataset is mirrored across engines).
3. **Guardrails** - AST validation feeds a cost gate that consults the planner
   before execution; failures return diagnostics, never data.
4. **Synthesis** - the final answer is grounded in scrubbed observations.

Self-healing contract: security, cost and execution failures are all returned
as observations containing the exact parser/planner message. The loop allows
``max_sql_repair_attempts`` rewrites, then stops with structured telemetry
rather than burning tokens indefinitely.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from time import perf_counter
from typing import Any, runtime_checkable
from typing import Protocol as ProtocolTyping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from datawhisper.core.config import DataWhisperConfig, LLMConfig
from datawhisper.core.database import (
    ExecutionEngine,
    QueryExecutionError,
    QueryTimeoutError,
)
from datawhisper.core.models import AgentTelemetry, RedactionReport
from datawhisper.core.schema_graph import JoinPlan, PrunedSchema, SchemaGraph
from datawhisper.core.security import CostViolation, SQLSecurityError

_MAX_OBSERVATION_CHARS = 3500
_MAX_ROWS_IN_OBSERVATION = 15

#: Tools return either a plain observation or (observation, redaction report).
ToolResult = str | tuple[str, RedactionReport]


# ---------------------------------------------------------------------------
# Tool inputs
# ---------------------------------------------------------------------------
class InspectSchemaInput(BaseModel):
    """Optionally narrow the inspection to one table."""

    table: str | None = None


class FindJoinPathInput(BaseModel):
    """Ask for the shortest FK join chain between two entities."""

    source_table: str
    target_table: str


class ProfileColumnInput(BaseModel):
    table: str
    column: str
    top_k: int = Field(default=8, ge=1, le=50)


class RunSqlInput(BaseModel):
    sql: str = Field(min_length=1)


class ExplainSqlInput(BaseModel):
    sql: str


class FinalAnswerInput(BaseModel):
    answer: str
    sql: str | None = None


class AgentStep(BaseModel):
    """One ReAct turn retained as an audit trail."""

    model_config = ConfigDict(frozen=True)

    index: int
    thought: str = ""
    action: str
    action_input: dict[str, Any] = Field(default_factory=dict)
    observation: str
    elapsed_ms: float


class AgentResult(BaseModel):
    """Outcome of one autonomous analysis, including safety telemetry."""

    question: str
    answer: str
    sql: str | None = None
    success: bool
    iterations: int
    steps: list[AgentStep] = Field(default_factory=list)
    telemetry: list[AgentTelemetry] = Field(default_factory=list)
    pruned_tables: list[str] = Field(default_factory=list)
    join_hops: int = 0
    redactions: RedactionReport | None = None
    elapsed_ms: float = 0.0


class AgentParseError(ValueError):
    """The model turn was not a valid agent JSON message."""


class ProviderError(RuntimeError):
    """The LLM provider failed, is unconfigured, or its SDK is missing."""


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
Message = Mapping[str, str]


@runtime_checkable
class LLMProvider(ProtocolTyping):
    def complete(self, messages: Sequence[Message]) -> str: ...


class OpenAIProvider:
    """OpenAI Chat Completions adapter (SDK imported lazily)."""

    def __init__(self, config: LLMConfig) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise ProviderError("OpenAI provider requires: pip install 'datawhisper[llm]'") from exc
        if not config.api_key:
            raise ProviderError("OPENAI_API_KEY is not set")
        self._model = config.resolve_model()
        self._temperature = config.temperature
        self._client = OpenAI(api_key=config.api_key)

    def complete(self, messages: Sequence[Message]) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            temperature=self._temperature,
            messages=[dict(message) for message in messages],
        )
        content = response.choices[0].message.content or ""
        if not content.strip():
            raise ProviderError("OpenAI returned an empty completion")
        return content


class GeminiProvider:
    """Google Gemini adapter (SDK imported lazily)."""

    def __init__(self, config: LLMConfig) -> None:
        try:
            import google.generativeai as genai
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise ProviderError("Gemini provider requires: pip install 'datawhisper[llm]'") from exc
        if not config.api_key:
            raise ProviderError("GEMINI_API_KEY is not set")
        genai.configure(api_key=config.api_key)
        self._genai = genai
        self._model_name = config.resolve_model()
        self._temperature = config.temperature

    def complete(self, messages: Sequence[Message]) -> str:
        system_chunks = [m["content"] for m in messages if m["role"] == "system"]
        contents = [
            {
                "role": "model" if m["role"] == "assistant" else "user",
                "parts": [m["content"]],
            }
            for m in messages
            if m["role"] != "system"
        ]
        model = self._genai.GenerativeModel(
            model_name=self._model_name,
            system_instruction="\n\n".join(system_chunks) or None,
        )
        response = model.generate_content(
            contents,
            generation_config=self._genai.GenerationConfig(temperature=self._temperature),
        )
        text = getattr(response, "text", "") or ""
        if not text.strip():
            raise ProviderError("Gemini returned an empty completion")
        return text


class ScriptedProvider:
    """Deterministic provider for tests and offline demos."""

    def __init__(self, responses: Sequence[str]) -> None:
        self._responses: deque[str] = deque(responses)

    def complete(self, messages: Sequence[Message]) -> str:
        if not self._responses:
            raise ProviderError("scripted provider exhausted")
        return self._responses.popleft()


def build_provider(config: LLMConfig) -> LLMProvider:
    if config.provider == "gemini":
        return GeminiProvider(config)
    if config.provider == "openai":
        return OpenAIProvider(config)
    raise ProviderError(
        "No LLM provider configured. Set GEMINI_API_KEY or OPENAI_API_KEY "
        "(or DW_LLM_PROVIDER) to enable the agentic layer."
    )


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
class DataWhisperAgent:
    """Graph-pruned, cost-gated, PII-safe SQL analyst."""

    def __init__(
        self,
        engine: ExecutionEngine,
        provider: LLMProvider,
        config: DataWhisperConfig | None = None,
    ) -> None:
        self._engine = engine
        self._provider = provider
        self._config = config or DataWhisperConfig()
        self._safety = self._config.safety
        self._graph: SchemaGraph | None = None
        self._pruned: PrunedSchema | None = None
        self._tools: dict[str, Callable[[dict[str, Any]], ToolResult]] = {
            "inspect_schema": self._tool_inspect_schema,
            "find_join_path": self._tool_find_join_path,
            "profile_column": self._tool_profile_column,
            "run_sql": self._tool_run_sql,
            "explain_sql": self._tool_explain,
        }

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def run(self, question: str) -> AgentResult:
        started = perf_counter()
        telemetry: list[AgentTelemetry] = []
        steps: list[AgentStep] = []
        repair_failures = 0
        format_failures = 0
        total_redactions = RedactionReport()

        try:
            catalog = self._engine.introspect()
            self._graph = SchemaGraph(catalog, self._config.schema_graph)
            self._pruned = self._graph.retrieve(question, self._config.schema_graph.max_tables)
        except Exception as exc:
            telemetry.append(
                AgentTelemetry(stage="schema_graph", outcome="failed", detail=str(exc))
            )
            return self._fail(
                question, telemetry, steps, started, f"Schema retrieval failed: {exc}"
            )

        telemetry.append(
            AgentTelemetry(
                stage="schema_graph",
                outcome="pruned",
                detail=f"{len(self._pruned.tables)} of {len(catalog.tables)} tables selected",
            )
        )
        context = self._pruned.render(self._config.schema_graph.max_context_tokens)

        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt(context)},
            {"role": "user", "content": question},
        ]

        for _ in range(self._safety.max_agent_iterations):
            try:
                raw = self._provider.complete(messages)
            except ProviderError as exc:
                telemetry.append(
                    AgentTelemetry(stage="provider", outcome="failed", detail=str(exc))
                )
                return self._fail(
                    question, telemetry, steps, started, f"LLM provider failed: {exc}"
                )

            try:
                action, payload = self._parse_message(raw)
            except AgentParseError as exc:
                format_failures += 1
                telemetry.append(
                    AgentTelemetry(stage="format", outcome="rejected", detail=str(exc))
                )
                if format_failures > 1:
                    return self._fail(
                        question,
                        telemetry,
                        steps,
                        started,
                        "Model repeatedly produced unparseable turns; aborting.",
                    )
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[FORMAT ERROR] {exc} Respond with exactly one JSON object: "
                            '{"thought": "...", "action": "<tool or final_answer>", '
                            '"action_input": {...}}'
                        ),
                    }
                )
                continue

            thought = str(payload.pop("thought", ""))

            if action == "final_answer":
                try:
                    final = FinalAnswerInput.model_validate(payload)
                except ValidationError as exc:
                    messages.append({"role": "assistant", "content": raw})
                    messages.append(
                        {"role": "user", "content": f"[FORMAT ERROR] final_answer invalid: {exc}"}
                    )
                    continue
                return AgentResult(
                    question=question,
                    answer=final.answer,
                    sql=final.sql,
                    success=True,
                    iterations=len(steps),
                    steps=steps,
                    telemetry=telemetry,
                    pruned_tables=self._pruned.table_names,
                    join_hops=self._pruned.join_plan.hops if self._pruned.join_plan else 0,
                    redactions=total_redactions if total_redactions.total else None,
                    elapsed_ms=round((perf_counter() - started) * 1000, 3),
                )

            step_started = perf_counter()
            observation, redactions = self._dispatch(action, payload, telemetry)
            if redactions is not None:
                total_redactions = total_redactions.merge(redactions)
            steps.append(
                AgentStep(
                    index=len(steps),
                    thought=thought,
                    action=action,
                    action_input=payload,
                    observation=observation,
                    elapsed_ms=round((perf_counter() - step_started) * 1000, 3),
                )
            )

            if (
                observation.startswith("[SECURITY ERROR]")
                or observation.startswith("[COST ERROR]")
                or observation.startswith("[SQL ERROR]")
                or observation.startswith("[TIMEOUT]")
            ):
                repair_failures += 1

            if repair_failures > self._safety.max_sql_repair_attempts:
                telemetry.append(
                    AgentTelemetry(
                        stage="self_healing",
                        outcome="exhausted",
                        detail=f"{repair_failures} failed attempts",
                    )
                )
                return self._fail(
                    question,
                    telemetry,
                    steps,
                    started,
                    f"Gave up after {repair_failures} failed attempts. "
                    "Parser, planner and driver diagnostics are in the step history.",
                )

            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": observation[:_MAX_OBSERVATION_CHARS]})

        telemetry.append(
            AgentTelemetry(stage="loop", outcome="exhausted", detail="iteration budget")
        )
        return self._fail(
            question,
            telemetry,
            steps,
            started,
            f"Iteration budget of {self._safety.max_agent_iterations} turns exhausted.",
        )

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------
    def _dispatch(
        self, action: str, payload: dict[str, Any], telemetry: list[AgentTelemetry]
    ) -> tuple[str, RedactionReport | None]:
        handler = self._tools.get(action)
        if handler is None:
            known = ", ".join(sorted(self._tools))
            return f"[TOOL ERROR] unknown action {action!r}; available: {known}", None
        try:
            result = handler(payload)
        except ValidationError as exc:
            return f"[TOOL ERROR] invalid input for {action}: {exc}", None
        except SQLSecurityError as exc:
            telemetry.append(
                AgentTelemetry(stage=action, outcome="blocked_by_sandbox", detail=str(exc)[:300])
            )
            return f"[SECURITY ERROR] {exc}", None
        except CostViolation as exc:
            telemetry.append(
                AgentTelemetry(stage=action, outcome="blocked_by_cost_gate", detail=str(exc)[:300])
            )
            cartesian = exc.decision.cartesian_groups
            hint = (
                " Add an explicit join predicate between every pair of tables."
                if cartesian
                else " Reduce the scanned data or add a selective filter."
            )
            return f"[COST ERROR] {exc}.{hint}", None
        except QueryTimeoutError as exc:
            telemetry.append(AgentTelemetry(stage=action, outcome="timeout", detail=str(exc)))
            return f"[TIMEOUT] {exc}", None
        except QueryExecutionError as exc:
            telemetry.append(AgentTelemetry(stage=action, outcome="driver_error", detail=str(exc)))
            return f"[SQL ERROR] {exc}", None
        except Exception as exc:
            telemetry.append(
                AgentTelemetry(stage=action, outcome="error", detail=f"{type(exc).__name__}: {exc}")
            )
            return f"[TOOL ERROR] {action} failed: {exc}", None
        if isinstance(result, tuple):
            return result
        return result, None

    def _tool_inspect_schema(self, payload: dict[str, Any]) -> str:
        args = InspectSchemaInput.model_validate(payload)
        if self._pruned is None:
            return "[TOOL ERROR] schema graph unavailable"
        tables = self._pruned.tables
        if args.table:
            lowered = args.table.lower()
            tables = [t for t in tables if t.name.lower() == lowered]
            if not tables:
                catalog = self._engine.introspect()
                full = catalog.table(args.table)
                if full is None:
                    return f"[TOOL ERROR] unknown table {args.table!r} in this dataset"
                return json.dumps(
                    {
                        "table": full.name,
                        "row_count": full.row_count,
                        "columns": [
                            {"name": c.name, "type": c.type, "nullable": c.nullable}
                            for c in full.columns[: self._config.schema_graph.max_columns_per_table]
                        ],
                        "note": "table was outside the pruned context; truncated for budget",
                    }
                )
        return json.dumps(
            {
                "tables": [
                    {
                        "name": t.name,
                        "row_count": t.row_count,
                        "columns": [
                            {
                                "name": c.name,
                                "type": c.type,
                                "pk": c.primary_key,
                                "fk": c.foreign_key,
                            }
                            for c in t.columns
                        ],
                    }
                    for t in tables
                ],
                "pruned_from": len(self._engine.introspect().tables),
            }
        )[:_MAX_OBSERVATION_CHARS]

    def _tool_find_join_path(self, payload: dict[str, Any]) -> str:
        args = FindJoinPathInput.model_validate(payload)
        if self._graph is None:
            return "[TOOL ERROR] schema graph unavailable"
        plan = self._graph.shortest_path(args.source_table, args.target_table)
        if plan is None:
            return (
                f"[TOOL ERROR] no FK path between {args.source_table!r} and "
                f"{args.target_table!r}; use an explicit join key or inspect both tables"
            )
        return _truncate(
            {
                "tables": plan.tables,
                "hops": plan.hops,
                "join_conditions": [step.sql_condition() for step in plan.steps],
            }
        )

    def _tool_profile_column(self, payload: dict[str, Any]) -> ToolResult:
        args = ProfileColumnInput.model_validate(payload)
        profile = self._engine.profile_column(args.table, args.column, top_k=args.top_k)
        observation = _truncate(profile.model_dump(exclude={"redactions"}))
        return observation, profile.redactions or RedactionReport()

    def _tool_run_sql(self, payload: dict[str, Any]) -> ToolResult:
        args = RunSqlInput.model_validate(payload)
        result = self._engine.execute(
            args.sql,
            validate=True,
            check_cost=True,
            timeout_s=self._safety.query_timeout_seconds,
            max_rows=self._safety.max_rows_returned,
        )
        observation = _truncate(
            {
                "columns": result.columns,
                "row_count": result.row_count,
                "truncated": result.truncated,
                "elapsed_ms": result.elapsed_ms,
                "cost_action": result.cost_action,
                "auto_limit": result.limit_applied,
                "rows": result.rows[:_MAX_ROWS_IN_OBSERVATION],
                "pii_redactions": result.redactions.by_category if result.redactions else {},
            }
        )
        return observation, result.redactions or RedactionReport()

    def _tool_explain(self, payload: dict[str, Any]) -> str:
        args = ExplainSqlInput.model_validate(payload)
        plan, decision = self._engine.explain(args.sql)
        return _truncate(
            {
                "verdict": decision.action,
                "allowed": decision.allowed,
                "reasons": decision.reasons,
                "estimated_scan_rows": decision.estimated_scan_rows,
                "full_scans": [
                    {"table": node.table, "rows": node.estimated_rows} for node in plan.full_scans
                ],
                "suggested_limit": decision.rewritten_sql,
                "plan": [node.model_dump() for node in plan.nodes[:20]],
            }
        )

    # ------------------------------------------------------------------
    # Prompting
    # ------------------------------------------------------------------
    def _system_prompt(self, schema_context: str) -> str:
        tool_docs = {
            "inspect_schema": '{"table": "<optional>"}',
            "find_join_path": '{"source_table": "...", "target_table": "..."}',
            "profile_column": '{"table": "...", "column": "...", "top_k": 8}',
            "run_sql": '{"sql": "<single read-only statement>"}',
            "explain_sql": '{"sql": "<statement>"}',
        }
        rendered_tools = "\n".join(f"- {name}: {payload}" for name, payload in tool_docs.items())
        return (
            f"You are DataWhisper, a zero-trust SQL analyst for a "
            f"{self._engine.backend.dialect} dataset.\n\n"
            "Safety contract (absolute):\n"
            "- Emit one read-only statement: SELECT or WITH...SELECT. "
            "Use the explain_sql tool to inspect a plan.\n"
            "- No semicolons, comments, PRAGMA/ATTACH/COPY/INSTALL, file readers, "
            "or catalog-qualified references; the sandbox rejects them.\n"
            "- The cost gate may reject cartesian joins and may paginate large scans.\n"
            "- Results are PII-scrubbed before you see them. Never attempt to recover "
            "redacted values or infer them; treat them as unavailable.\n"
            "- Every number in the final answer must come from a tool observation.\n\n"
            f"Only this pruned sub-schema is in scope (selected for this question):\n"
            f"{schema_context}\n\n"
            f"Tools (JSON action_input):\n{rendered_tools}\n\n"
            "Respond with exactly ONE JSON object per turn: "
            '{"thought": "...", "action": "<tool or final_answer>", "action_input": {...}}\n'
            'For the final turn: {"thought": "...", "action": "final_answer", '
            '"action_input": {"answer": "<findings with concrete numbers>", '
            '"sql": "<SQL or null>"}}\n\n'
            "If a tool returns [SECURITY ERROR], [COST ERROR], [SQL ERROR] or [TIMEOUT], "
            "read the diagnostic, fix the statement and retry. You have at most "
            f"{self._safety.max_sql_repair_attempts} repair attempts."
        )

    # ------------------------------------------------------------------
    # Parsing and failure shaping
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_message(raw: str) -> tuple[str, dict[str, Any]]:
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        brace = text.find("{")
        if brace < 0:
            raise AgentParseError("no JSON object found in the model turn")
        try:
            payload, _ = json.JSONDecoder().raw_decode(text[brace:])
        except json.JSONDecodeError as exc:
            raise AgentParseError(f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict) or "action" not in payload:
            raise AgentParseError("JSON object must contain an 'action' key")
        action = str(payload["action"])
        action_input = payload.get("action_input", {})
        if isinstance(action_input, str):
            try:
                action_input = json.loads(action_input)
            except json.JSONDecodeError:
                action_input = {"value": action_input}
        if not isinstance(action_input, dict):
            raise AgentParseError("'action_input' must be an object")
        return action, dict(action_input)

    def _fail(
        self,
        question: str,
        telemetry: list[AgentTelemetry],
        steps: list[AgentStep],
        started: float,
        message: str,
    ) -> AgentResult:
        return AgentResult(
            question=question,
            answer=message,
            success=False,
            iterations=len(steps),
            steps=steps,
            telemetry=telemetry,
            pruned_tables=self._pruned.table_names if self._pruned else [],
            join_hops=self._pruned.join_plan.hops if self._pruned and self._pruned.join_plan else 0,
            elapsed_ms=round((perf_counter() - started) * 1000, 3),
        )


def _truncate(payload: Any) -> str:
    text = json.dumps(payload, default=str)
    return text[:_MAX_OBSERVATION_CHARS]


def join_plan_sql(plan: JoinPlan, left: str, right: str) -> str:
    """Render a ``FROM``/``JOIN`` fragment for a computed FK path.

    Exposed for the CLI and tests; the agent receives the same information as
    text through the pruned-schema prompt.
    """
    if not plan.steps:
        return left
    clauses = [f"FROM {plan.tables[0]}"]
    for step in plan.steps:
        clauses.append(f"JOIN {step.right_table} ON {step.sql_condition()}")
    return " ".join(clauses)
