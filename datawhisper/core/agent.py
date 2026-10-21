"""Zero-framework ReAct agent with a self-correcting SQL execution loop.

Design rationale
----------------
LangChain-style orchestration is intentionally avoided: the loop below is
~200 lines of plain Python, which keeps latency, token budgets and failure
modes fully observable. Each turn the model returns exactly one JSON object
(``thought`` / ``action`` / ``action_input``); the agent dispatches the tool,
appends the observation, and repeats until ``final_answer`` or a budget
expires.

Self-healing contract: when ``run_sql`` fails (syntax error, missing column,
division by zero, type mismatch) the driver's error message is returned to
the model verbatim as the observation, which is what allows it to repair the
SQL. After ``safety.max_sql_repair_attempts`` consecutive failures the agent
stops instead of burning tokens, and reports the accumulated error telemetry.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any, runtime_checkable
from typing import Protocol as ProtocolTyping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from datawhisper.core.config import DataWhisperConfig, LLMConfig
from datawhisper.core.database import ExecutionEngine, QueryTimeoutError
from datawhisper.core.security import SQLSecurityError

_MAX_OBSERVATION_CHARS = 4000
_MAX_ROWS_IN_OBSERVATION = 20


# ---------------------------------------------------------------------------
# Tool input schemas (Pydantic doubles as the validation and prompt layer)
# ---------------------------------------------------------------------------
class InspectSchemaInput(BaseModel):
    """List tables and columns of the attached dataset."""

    table: str | None = Field(default=None, description="Restrict output to one table.")


class ProfileColumnInput(BaseModel):
    """Compute null ratio, distinct count, top values and numeric statistics."""

    table: str
    column: str
    top_k: int = Field(default=8, ge=1, le=50)


class RunSqlInput(BaseModel):
    """Execute one read-only SQL statement through the sandbox."""

    sql: str = Field(min_length=1, description="A single SELECT/WITH/EXPLAIN statement.")


class ExplainSqlInput(BaseModel):
    """Show the driver's query plan for a statement."""

    sql: str


class FinalAnswerInput(BaseModel):
    """Terminate the loop with an executive summary grounded in observations."""

    answer: str
    sql: str | None = None


class AgentStep(BaseModel):
    """One ReAct turn, preserved for audit trails and timeline charts."""

    model_config = ConfigDict(frozen=True)

    index: int
    thought: str = ""
    action: str
    action_input: dict[str, Any] = Field(default_factory=dict)
    observation: str
    elapsed_ms: float


class AgentResult(BaseModel):
    """Final outcome of one autonomous analysis run."""

    question: str
    answer: str
    sql: str | None = None
    success: bool
    iterations: int
    steps: list[AgentStep] = Field(default_factory=list)
    telemetry: list[str] = Field(default_factory=list)
    elapsed_ms: float = 0.0


class AgentParseError(ValueError):
    """The model's turn was not a valid agent JSON message."""


class ProviderError(RuntimeError):
    """An LLM provider failed, is unconfigured, or its SDK is missing."""


# ---------------------------------------------------------------------------
# Provider interface: one Protocol, three implementations
# ---------------------------------------------------------------------------
Message = Mapping[str, str]


@runtime_checkable
class LLMProvider(ProtocolTyping):
    """Minimal chat-completion contract shared by every backend."""

    def complete(self, messages: Sequence[Message]) -> str: ...


class OpenAIProvider:
    """OpenAI Chat Completions adapter (SDK lazily imported, optional extra)."""

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
    """Google Gemini adapter (SDK lazily imported, optional extra)."""

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
    """Deterministic provider for tests and offline demos; pops queued replies."""

    def __init__(self, responses: Sequence[str]) -> None:
        self._responses: deque[str] = deque(responses)

    def complete(self, messages: Sequence[Message]) -> str:
        if not self._responses:
            raise ProviderError("scripted provider exhausted")
        return self._responses.popleft()


def build_provider(config: LLMConfig) -> LLMProvider:
    """Factory mapping configuration to a concrete provider instance."""
    if config.provider == "gemini":
        return GeminiProvider(config)
    if config.provider == "openai":
        return OpenAIProvider(config)
    raise ProviderError(
        "No LLM provider configured. Set GEMINI_API_KEY or OPENAI_API_KEY "
        "(or DW_LLM_PROVIDER) to enable the agentic layer."
    )


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[dict[str, Any]], str]


class DataWhisperAgent:
    """Self-correcting SQL analyst bound to one engine and one provider."""

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
        self._tools: dict[str, _Tool] = {
            "inspect_schema": _Tool(
                name="inspect_schema",
                description="List tables and columns of the attached dataset.",
                input_model=InspectSchemaInput,
                handler=self._tool_inspect_schema,
            ),
            "profile_column": _Tool(
                name="profile_column",
                description=(
                    "Null ratio, distinct count, top values and numeric stats for a column."
                ),
                input_model=ProfileColumnInput,
                handler=self._tool_profile_column,
            ),
            "run_sql": _Tool(
                name="run_sql",
                description=(
                    "Execute one read-only SQL statement (SELECT/WITH/EXPLAIN only) "
                    "and return up to 20 result rows."
                ),
                input_model=RunSqlInput,
                handler=self._tool_run_sql,
            ),
            "explain_sql": _Tool(
                name="explain_sql",
                description="Show the driver query plan for a statement.",
                input_model=ExplainSqlInput,
                handler=self._tool_explain,
            ),
        }

    # ------------------------------------------------------------------
    # ReAct loop
    # ------------------------------------------------------------------
    def run(self, question: str) -> AgentResult:
        """Answer one question, self-healing SQL failures along the way."""
        started = perf_counter()
        telemetry: list[str] = []
        steps: list[AgentStep] = []
        sql_failures = 0
        format_failures = 0

        system_prompt = self._system_prompt()
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"{question}\n\nDataset catalog:\n{self._catalog_digest()}",
            },
        ]

        for turn in range(self._safety.max_agent_iterations):
            try:
                raw = self._provider.complete(messages)
            except ProviderError as exc:
                telemetry.append(f"provider failure: {exc}")
                return self._fail(
                    question, telemetry, steps, started, f"LLM provider failed: {exc}"
                )

            try:
                parsed_action, parsed_input = self._parse_message(raw)
            except AgentParseError as exc:
                format_failures += 1
                telemetry.append(f"unparseable model turn #{turn}: {exc}")
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

            thought = str(parsed_input.pop("thought", ""))
            if parsed_action == "final_answer":
                try:
                    final = FinalAnswerInput.model_validate(parsed_input)
                except ValidationError as exc:
                    messages.append({"role": "assistant", "content": raw})
                    messages.append(
                        {
                            "role": "user",
                            "content": f"[FORMAT ERROR] final_answer payload invalid: {exc}",
                        }
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
                    elapsed_ms=round((perf_counter() - started) * 1000, 3),
                )

            step_started = perf_counter()
            observation = self._dispatch(parsed_action, parsed_input, telemetry)
            elapsed = round((perf_counter() - step_started) * 1000, 3)
            steps.append(
                AgentStep(
                    index=len(steps),
                    thought=thought,
                    action=parsed_action,
                    action_input=parsed_input,
                    observation=observation,
                    elapsed_ms=elapsed,
                )
            )

            if parsed_action == "run_sql" and observation.startswith("[TOOL ERROR]"):
                sql_failures += 1
                if sql_failures > self._safety.max_sql_repair_attempts:
                    telemetry.append(
                        f"self-repair budget exhausted after {sql_failures} failed SQL attempts"
                    )
                    return self._fail(
                        question,
                        telemetry,
                        steps,
                        started,
                        f"Gave up after {sql_failures} failed SQL attempts. "
                        "The sandbox and driver errors are attached in the step history.",
                    )

            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": observation[:_MAX_OBSERVATION_CHARS]})

        return self._fail(
            question,
            telemetry,
            steps,
            started,
            f"Iteration budget of {self._safety.max_agent_iterations} turns exhausted.",
        )

    # ------------------------------------------------------------------
    # Tools (observations are plain strings destined for the next prompt)
    # ------------------------------------------------------------------
    def _dispatch(self, action: str, raw_input: dict[str, Any], telemetry: list[str]) -> str:
        tool = self._tools.get(action)
        if tool is None:
            known = ", ".join(sorted(self._tools))
            return f"[TOOL ERROR] unknown action {action!r}; available tools: {known}"
        try:
            return tool.handler(raw_input)
        except ValidationError as exc:
            return f"[TOOL ERROR] invalid input for {action}: {exc}"
        except Exception as exc:
            telemetry.append(f"{action} raised {type(exc).__name__}: {exc}")
            return f"[TOOL ERROR] {action} failed: {exc}"

    def _tool_inspect_schema(self, raw: dict[str, Any]) -> str:
        args = InspectSchemaInput.model_validate(raw)
        snapshot = self._engine.introspect()
        tables = snapshot.tables
        if args.table:
            tables = [t for t in tables if t.name == args.table]
            if not tables:
                return f"[TOOL ERROR] unknown table {args.table!r}"
        payload = {
            "dialect": snapshot.dialect,
            "tables": [
                {
                    "name": t.name,
                    "row_count": t.row_count,
                    "columns": [
                        {"name": c.name, "type": c.type, "nullable": c.nullable} for c in t.columns
                    ],
                }
                for t in tables
            ],
        }
        return _truncate_payload(payload)

    def _tool_profile_column(self, raw: dict[str, Any]) -> str:
        args = ProfileColumnInput.model_validate(raw)
        try:
            profile = self._engine.profile_column(args.table, args.column, top_k=args.top_k)
        except ValueError as exc:
            return f"[TOOL ERROR] profile_column failed: {exc}"
        return _truncate_payload(profile.model_dump())

    def _tool_run_sql(self, raw: dict[str, Any]) -> str:
        args = RunSqlInput.model_validate(raw)
        try:
            result = self._engine.execute(
                args.sql,
                validate=True,
                timeout_s=self._safety.query_timeout_seconds,
                max_rows=self._safety.max_rows_returned,
            )
        except SQLSecurityError as exc:
            return f"[TOOL ERROR] run_sql blocked by sandbox: {exc}"
        except QueryTimeoutError as exc:
            return f"[TOOL ERROR] run_sql timed out: {exc}"
        except Exception as exc:  # driver errors feed the self-healing loop
            return f"[TOOL ERROR] run_sql failed: {exc}"
        payload = {
            "columns": result.columns,
            "row_count": result.row_count,
            "truncated": result.truncated,
            "elapsed_ms": result.elapsed_ms,
            "rows": result.rows[:_MAX_ROWS_IN_OBSERVATION],
        }
        return _truncate_payload(payload)

    def _tool_explain(self, raw: dict[str, Any]) -> str:
        args = ExplainSqlInput.model_validate(raw)
        try:
            result = self._engine.explain(args.sql)
        except Exception as exc:
            return f"[TOOL ERROR] explain_sql failed: {exc}"
        return _truncate_payload({"plan": result.rows[:_MAX_ROWS_IN_OBSERVATION]})

    # ------------------------------------------------------------------
    # Prompt assembly and message parsing
    # ------------------------------------------------------------------
    def _system_prompt(self) -> str:
        tool_docs = "\n".join(
            f"- {tool.name}: {tool.description} input_schema="
            f"{json.dumps(tool.input_model.model_json_schema()['properties'])}"
            for tool in self._tools.values()
        )
        return (
            f"You are DataWhisper, an autonomous SQL analyst operating against a "
            f"{self._engine.backend.dialect} database.\n\n"
            "Safety contract (absolute):\n"
            "- Emit only read-only SQL: a single SELECT, WITH...SELECT, or EXPLAIN statement.\n"
            "- Never use PRAGMA, ATTACH, COPY, INSTALL, file-reading functions, semicolons "
            "or comments; the sandbox rejects them.\n"
            "- Never fabricate numbers: every figure in your final answer must come from "
            "a tool observation.\n\n"
            f"Available tools:\n{tool_docs}\n\n"
            "Respond with exactly ONE JSON object per turn:\n"
            '{"thought": "<brief reasoning>", "action": "<tool_name or final_answer>", '
            '"action_input": {...}}\n'
            'For final_answer use {"thought": "...", "action": "final_answer", '
            '"action_input": {"answer": "<executive summary with concrete numbers>", '
            '"sql": "<final SQL or null>"}}\n\n'
            "Workflow: inspect the schema first, profile columns when semantics are "
            "unclear, then run SQL. If a tool returns [TOOL ERROR], read the driver "
            "message, fix the SQL and retry. You have at most "
            f"{self._safety.max_sql_repair_attempts} SQL repair attempts."
        )

    def _catalog_digest(self) -> str:
        try:
            snapshot = self._engine.introspect()
        except Exception:
            return "(catalog unavailable)"
        lines = []
        for table in snapshot.tables[:60]:
            columns = ", ".join(f"{c.name} {c.type}" for c in table.columns[:40])
            lines.append(f"- {table.name}({columns}) rows={table.row_count}")
        return "\n".join(lines)[:4000] or "(no tables found)"

    @staticmethod
    def _parse_message(raw: str) -> tuple[str, dict[str, Any]]:
        """Extract ``(action, action_input)`` from a model turn.

        Tolerates markdown code fences and leading prose; uses a balanced
        ``raw_decode`` from the first ``{`` so nested braces survive.
        """
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
        telemetry: list[str],
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
            elapsed_ms=round((perf_counter() - started) * 1000, 3),
        )


def _truncate_payload(payload: Any) -> str:
    """Serialize an observation, hard-capped so prompts stay within budget."""
    text = json.dumps(payload, default=str)
    return text[:_MAX_OBSERVATION_CHARS]
