"""Pydantic-based configuration with environment loading and safety thresholds.

All runtime knobs are gathered in one immutable, validated object so that the
agent, the execution engine and the GitHub bot share a single source of truth.
Environment variables provide 12-factor style configuration for CI runners,
where no interactive setup is possible.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ProviderName = Literal["gemini", "openai", "none"]


class SafetyThresholds(BaseModel):
    """Guardrails that bound the blast radius of any single agent action.

    ``query_timeout_seconds`` and ``max_rows_returned`` protect the host from
    resource exhaustion; ``max_sql_repair_attempts`` bounds the self-healing
    loop so a persistently failing model cannot loop forever.
    """

    model_config = ConfigDict(frozen=True)

    query_timeout_seconds: float = Field(
        default=10.0, gt=0, description="Per-query wall-clock budget."
    )
    max_rows_returned: int = Field(
        default=1000, gt=0, description="Hard cap on rows fetched per query."
    )
    max_sql_repair_attempts: int = Field(
        default=3, ge=1, description="Self-healing retries before the agent gives up."
    )
    max_agent_iterations: int = Field(
        default=8, ge=1, description="Total ReAct turns per question."
    )
    max_column_sample_values: int = Field(
        default=12, ge=1, le=100, description="Distinct values sampled per profile."
    )


class LLMConfig(BaseModel):
    """Unified provider configuration; SDK clients are created lazily."""

    model_config = ConfigDict(frozen=True)

    provider: ProviderName = "none"
    model: str = ""
    api_key: str = ""
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)

    def resolve_model(self) -> str:
        """Fall back to a sensible per-provider default when unset."""
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
    """Root configuration object assembled from defaults plus environment."""

    model_config = ConfigDict(frozen=True)

    llm: LLMConfig = Field(default_factory=LLMConfig)
    safety: SafetyThresholds = Field(default_factory=SafetyThresholds)
    github: GitHubConfig = Field(default_factory=GitHubConfig)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DataWhisperConfig:
        """Build a frozen configuration from ``os.environ`` (or an injected map).

        Provider auto-detection order: explicit ``DW_LLM_PROVIDER`` wins, then
        ``GEMINI_API_KEY``/``GOOGLE_API_KEY`` (gemini), then ``OPENAI_API_KEY``.
        When no key is present the provider stays ``none`` and the deterministic
        audit path (schema drift + benchmarks, no LLM) remains fully usable.
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

        return cls(
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
            ),
            github=GitHubConfig(
                token=source.get("DATAWHISPER_GITHUB_TOKEN") or source.get("GITHUB_TOKEN") or "",
                repo=source.get("DW_GITHUB_REPO", ""),
                api_base=source.get("DW_GITHUB_API_BASE", "https://api.github.com"),
            ),
        )
