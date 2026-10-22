"""Pydantic configuration: provider settings, safety budgets and policy knobs.

Every runtime decision that bounds cost or controls disclosure is expressed
here as an immutable, validated object. CI reads the same object from the
environment, so a pull-request audit cannot accidentally run with different
limits than a local session.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ProviderName = Literal["gemini", "openai", "local", "none"]
DecisionProvider = Literal["auto", "heuristic", "onnx", "http"]
RedactionMode = Literal["hash", "mask", "tokenize", "redact"]
PlanFailurePolicy = Literal["allow_with_budget", "reject"]


class DecisionConfig(BaseModel):
    """System-1 decision engine selection and firewall enforcement."""

    model_config = ConfigDict(frozen=True)

    provider: DecisionProvider = "auto"
    model_path: str = ""
    endpoint: str = ""
    request_timeout: float = Field(default=0.25, gt=0)
    risk_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    firewall_enabled: bool = True


class SafetyThresholds(BaseModel):
    """Hard bounds applied by the execution engine, independent of any plan."""

    model_config = ConfigDict(frozen=True)

    query_timeout_seconds: float = Field(default=10.0, gt=0)
    max_rows_returned: int = Field(default=1000, gt=0)
    max_sql_repair_attempts: int = Field(default=3, ge=1)
    max_agent_iterations: int = Field(default=8, ge=1)
    max_column_sample_values: int = Field(default=12, ge=1, le=100)


class CostPolicy(BaseModel):
    """Cost gatekeeper thresholds.

    ``max_scan_rows`` is compared against the planner's estimate (or catalog
    row counts when the planner omits estimates). When a statement would scan
    more than that without a bound, the gate injects ``auto_limit`` rather
    than rejecting outright, because a paginated answer is still useful.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    max_scan_rows: int = Field(default=5_000_000, ge=0)
    auto_limit: int = Field(default=500, ge=1)
    force_limit_for_large_scans: bool = True
    reject_cartesian_joins: bool = True
    plan_failure_policy: PlanFailurePolicy = "allow_with_budget"


class PrivacyConfig(BaseModel):
    """PII scrubber configuration.

    ``salt`` must be stable across CI runs when ``hash`` mode is used,
    otherwise the same email produces different pseudonyms on every run and
    trend comparisons break. Set ``DW_PII_SALT`` as a repository secret.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    mode: RedactionMode = "hash"
    salt: str = "datawhisper-local-salt"
    hash_length: int = Field(default=16, ge=8, le=64)
    column_allowlist: tuple[str, ...] = ()


class SchemaGraphConfig(BaseModel):
    """Context-pruning budget for the LLM prompt."""

    model_config = ConfigDict(frozen=True)

    max_tables: int = Field(default=6, ge=1)
    include_neighbor_tables: int = Field(default=2, ge=0)
    max_columns_per_table: int = Field(default=12, ge=1)
    max_context_tokens: int = Field(default=1500, ge=200)
    bm25_k1: float = Field(default=1.5, gt=0)
    bm25_b: float = Field(default=0.75, ge=0, le=1)
    domain_pruning: bool = Field(
        default=True,
        description="Coarse-to-fine: pick domains first, then BM25 within them.",
    )
    max_domains: int = Field(default=3, ge=1, le=20)


class LLMConfig(BaseModel):
    """Unified provider configuration; SDK clients are created lazily."""

    model_config = ConfigDict(frozen=True)

    provider: ProviderName = "none"
    model: str = ""
    api_key: str = ""
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)

    def resolve_model(self) -> str:
        if self.model:
            return self.model
        return "gemini-2.0-flash" if self.provider == "gemini" else "gpt-4o-mini"


class GitHubConfig(BaseModel):
    """GitHub REST API settings; the token is injected via env in CI."""

    model_config = ConfigDict(frozen=True)

    token: str = ""
    repo: str = ""
    api_base: str = "https://api.github.com"
    request_timeout: float = Field(default=20.0, gt=0)


class DataWhisperConfig(BaseModel):
    """Root configuration assembled from defaults plus environment overrides."""

    model_config = ConfigDict(frozen=True)

    llm: LLMConfig = Field(default_factory=LLMConfig)
    decision: DecisionConfig = Field(default_factory=DecisionConfig)
    safety: SafetyThresholds = Field(default_factory=SafetyThresholds)
    cost: CostPolicy = Field(default_factory=CostPolicy)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    schema_graph: SchemaGraphConfig = Field(default_factory=SchemaGraphConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DataWhisperConfig:
        """Build a frozen configuration from ``os.environ`` or an injected map.

        Provider auto-detection order: explicit ``DW_LLM_PROVIDER`` wins, then
        ``GEMINI_API_KEY``/``GOOGLE_API_KEY``, then ``OPENAI_API_KEY``. With no
        key the provider stays ``none`` and the deterministic audit path
        (drift, PII scan, plans) remains fully usable.
        """
        source = dict(os.environ) if env is None else dict(env)

        provider = source.get("DW_LLM_PROVIDER", "").strip().lower()
        gemini_key = source.get("GEMINI_API_KEY") or source.get("GOOGLE_API_KEY") or ""
        openai_key = source.get("OPENAI_API_KEY") or ""
        if provider not in {"gemini", "openai", "none"}:
            provider = "gemini" if gemini_key else ("openai" if openai_key else "none")
        api_key = (
            gemini_key if provider == "gemini" else (openai_key if provider == "openai" else "")
        )

        def _float(name: str, default: float) -> float:
            try:
                return float(source[name])
            except (KeyError, ValueError):
                return default

        def _int(name: str, default: int) -> int:
            try:
                return int(source[name])
            except (KeyError, ValueError):
                return default

        def _bool(name: str, default: bool) -> bool:
            raw = source.get(name)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        mode = source.get("DW_PII_MODE", "hash").strip().lower()
        if mode not in {"hash", "mask", "tokenize", "redact"}:
            mode = "hash"

        plan_policy = source.get("DW_PLAN_FAILURE_POLICY", "allow_with_budget").strip().lower()
        if plan_policy not in {"allow_with_budget", "reject"}:
            plan_policy = "allow_with_budget"

        allow_columns = tuple(
            column.strip()
            for column in source.get("DW_PII_ALLOW_COLUMNS", "").split(",")
            if column.strip()
        )

        decision_provider = source.get("DW_DECISION_PROVIDER", "auto").strip().lower()
        if decision_provider not in {"auto", "heuristic", "onnx", "http"}:
            decision_provider = "auto"
        risk_threshold = _float("DW_RISK_THRESHOLD", 0.7)

        return cls(
            decision=DecisionConfig(
                provider=decision_provider,  # type: ignore[arg-type]
                model_path=source.get("DW_DECISION_MODEL_PATH", ""),
                endpoint=source.get("DW_LAYA_ENDPOINT", ""),
                request_timeout=_float("DW_DECISION_TIMEOUT", 0.25),
                risk_threshold=min(max(risk_threshold, 0.0), 1.0),
                firewall_enabled=_bool("DW_FIREWALL_ENABLED", True),
            ),
            llm=LLMConfig(
                provider=provider,  # type: ignore[arg-type]
                model=source.get("DW_LLM_MODEL", ""),
                api_key=api_key,
                temperature=_float("DW_LLM_TEMPERATURE", 0.1),
            ),
            safety=SafetyThresholds(
                query_timeout_seconds=_float("DW_QUERY_TIMEOUT", 10.0),
                max_rows_returned=_int("DW_MAX_ROWS", 1000),
                max_sql_repair_attempts=_int("DW_MAX_REPAIRS", 3),
                max_agent_iterations=_int("DW_MAX_ITERATIONS", 8),
                max_column_sample_values=_int("DW_MAX_SAMPLES", 12),
            ),
            cost=CostPolicy(
                enabled=_bool("DW_COST_ENABLED", True),
                max_scan_rows=_int("DW_MAX_SCAN_ROWS", 5_000_000),
                auto_limit=_int("DW_AUTO_LIMIT", 500),
                force_limit_for_large_scans=_bool("DW_FORCE_LIMIT", True),
                reject_cartesian_joins=_bool("DW_REJECT_CARTESIAN", True),
                plan_failure_policy=plan_policy,  # type: ignore[arg-type]
            ),
            privacy=PrivacyConfig(
                enabled=_bool("DW_PII_ENABLED", True),
                mode=mode,  # type: ignore[arg-type]
                salt=source.get("DW_PII_SALT", "datawhisper-local-salt"),
                hash_length=_int("DW_PII_HASH_LENGTH", 16),
                column_allowlist=allow_columns,
            ),
            schema_graph=SchemaGraphConfig(
                max_tables=_int("DW_GRAPH_MAX_TABLES", 6),
                include_neighbor_tables=_int("DW_GRAPH_NEIGHBORS", 2),
                max_columns_per_table=_int("DW_GRAPH_MAX_COLUMNS", 12),
                max_context_tokens=_int("DW_GRAPH_MAX_TOKENS", 1500),
                bm25_k1=_float("DW_BM25_K1", 1.5),
                bm25_b=_float("DW_BM25_B", 0.75),
            ),
            github=GitHubConfig(
                token=source.get("DATAWHISPER_GITHUB_TOKEN") or source.get("GITHUB_TOKEN") or "",
                repo=source.get("DW_GITHUB_REPO", ""),
                api_base=source.get("DW_GITHUB_API_BASE", "https://api.github.com"),
                request_timeout=_float("DW_GITHUB_TIMEOUT", 20.0),
            ),
        )
