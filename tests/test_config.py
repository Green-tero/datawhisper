"""Tests for DataWhisper configuration parsing from environment variables."""

from __future__ import annotations

from datawhisper.core.config import DataWhisperConfig, LLMConfig


def test_config_from_env_defaults() -> None:
    config = DataWhisperConfig.from_env(env={})
    assert config.privacy.enabled is True
    assert config.privacy.mode == "hash"
    assert config.schema_graph.domain_pruning is True
    assert config.schema_graph.max_domains == 3
    assert config.schema_graph.hybrid_dense_pruning is True
    assert config.decision.provider == "auto"


def test_config_differential_privacy_mode() -> None:
    env = {
        "DW_PII_MODE": "differential_privacy",
        "DW_PII_DP_EPSILON": "0.5",
        "DW_PII_DP_DELTA": "1e-6",
        "DW_PII_DP_CLAMP_MIN": "0.0",
        "DW_PII_DP_CLAMP_MAX": "100.0",
        "DW_PII_DP_SEED": "123",
    }
    config = DataWhisperConfig.from_env(env=env)
    assert config.privacy.mode == "differential_privacy"
    assert config.privacy.dp.epsilon == 0.5
    assert config.privacy.dp.delta == 1e-6
    assert config.privacy.dp.clamp_min == 0.0
    assert config.privacy.dp.clamp_max == 100.0
    assert config.privacy.dp.seed == 123


def test_config_schema_graph_domain_pruning() -> None:
    env = {
        "DW_DOMAIN_PRUNING": "false",
        "DW_MAX_DOMAINS": "7",
        "DW_GRAPH_HYBRID_DENSE": "false",
        "DW_GRAPH_HYBRID_WEIGHT": "0.55",
        "DW_GRAPH_MAX_TABLES": "10",
    }
    config = DataWhisperConfig.from_env(env=env)
    assert config.schema_graph.domain_pruning is False
    assert config.schema_graph.max_domains == 7
    assert config.schema_graph.hybrid_dense_pruning is False
    assert config.schema_graph.hybrid_dense_weight == 0.55
    assert config.schema_graph.max_tables == 10


def test_config_hmac_salt_precedence() -> None:
    env = {
        "DW_PII_HMAC_SALT": "session-secret-salt-xyz",
        "DW_PII_SALT": "ignored-salt",
    }
    config = DataWhisperConfig.from_env(env=env)
    assert config.privacy.salt == "session-secret-salt-xyz"


def test_config_gemini_2026_model_default() -> None:
    llm = LLMConfig(provider="gemini")
    assert llm.resolve_model() == "gemini-3-flash"
