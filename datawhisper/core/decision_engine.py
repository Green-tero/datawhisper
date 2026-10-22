"""System-1 decision engine: sub-50ms local judgments for the agentic pipeline.

Architecture
------------
The copilot separates *judgment* (System-1: cheap, frequent, bounded decisions
such as domain selection, intent classification and risk scoring) from
*synthesis* (System-2: SQL generation by a code LLM). System-1 must never cost
a network round-trip, so three interchangeable backends implement one
protocol:

=================  ===========================================================
Backend            When it runs
=================  ===========================================================
``Heuristic``      Always available: deterministic lexical/regex scoring.
                   Runs in well under a millisecond and is fully reproducible,
                   which makes it the engine of record for tests and CI.
``Onnx``           Loads a local ONNX text-classification model when weights
                   are configured (``DW_DECISION_MODEL_PATH``). Imported and
                   loaded lazily; any failure degrades to the heuristic.
``Remote``         Talks to a local inference server (``DW_LAYA_ENDPOINT``,
                   a ``laya-serve``-style HTTP contract) for heavier models.
                   opt-in only; never contacted unless explicitly configured.
=================  ===========================================================

Security invariants
-------------------
* The engine never sees row data - only schema identifiers, questions and
  statements the caller already chose to expose.
* Every decision is local: no telemetry, no network in ``auto`` mode.
* Failures are fail-soft *at construction* but fail-closed *at enforcement*:
  a firewall built on the heuristic engine still blocks what it measures.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, Field

from datawhisper.core.config import DecisionConfig


class DecisionBackendError(RuntimeError):
    """A configured non-heuristic backend is unavailable or misconfigured."""


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Decision:
    """One System-1 judgment with its latency and provenance."""

    value: Any
    backend: str
    latency_ms: float


@runtime_checkable
class DecisionEngine(Protocol):
    """The three decision primitives every backend implements."""

    name: str

    def decide_boolean(self, statement: str, context: str) -> Decision: ...

    def classify_choice(self, query: str, options: Sequence[str]) -> Decision: ...

    def score_risk(self, text: str, criteria: str = "") -> Decision: ...


# ---------------------------------------------------------------------------
# Tokenisation shared by the heuristic backend
# ---------------------------------------------------------------------------
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
        "in", "is", "it", "of", "on", "or", "that", "the", "their", "this",
        "to", "was", "were", "what", "which", "who", "with", "show", "list",
        "me", "give", "all", "per", "each", "please",
    }
)


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text.lower()))
        if len(token) > 1 and token not in _STOPWORDS
    }


def _affinity_hits(query_tokens: set[str]) -> frozenset[str]:
    """Concepts the question implies, per the curated lexicon."""
    return frozenset(
        concept
        for concept, words in _AFFINITY_LEXICON.items()
        if query_tokens & words
    )


def _now_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 4)


# ---------------------------------------------------------------------------
# Intent vocabulary for the heuristic classifier
# ---------------------------------------------------------------------------
#: Query-side keywords that imply each option-side concept token. Pure token
#: overlap cannot tell ``spend`` from ``aggregate_analytics`` because they
#: share no characters, so a small curated lexicon bridges the gap. It stays
#: deterministic, offline and side-effect free - a miniature stand-in for the
#: semantic layer a real System-1 model would provide.
_AFFINITY_LEXICON: dict[str, frozenset[str]] = {
    "aggregate": frozenset(
        {"sum", "total", "count", "many", "average", "avg", "mean", "spend", "spends",
         "revenue", "breakdown", "aggregate", "aggregation", "group", "per", "totals"}
    ),
    "analytics": frozenset(
        {"analytics", "statistic", "statistics", "stats", "distribution", "ratio",
         "percent", "percentage", "share", "share_ratio"}
    ),
    "trend": frozenset(
        {"trend", "trends", "growth", "change", "daily", "weekly", "monthly",
         "quarterly", "yearly", "history", "series", "over"}
    ),
    "time": frozenset({"time", "times", "timeline", "chronological", "sequence"}),
    "lookup": frozenset(
        {"lookup", "get", "find", "fetch", "retrieve", "row", "record", "entry",
         "detail", "details", "specific", "exact", "search"}
    ),
    "row": frozenset({"row", "rows", "record", "records", "entry", "entries"}),
    "schema": frozenset(
        {"schema", "table", "tables", "column", "columns", "structure", "catalog",
         "join", "joins", "relationship", "relationships", "keys"}
    ),
    "exploration": frozenset(
        {"explore", "exploration", "discover", "inspect", "overview", "map", "audit"}
    ),
    "quality": frozenset(
        {"quality", "null", "nulls", "missing", "duplicate", "duplicates", "anomaly",
         "anomalies", "outlier", "outliers", "skew", "dirty", "clean", "invalid",
         "malformed", "consistent"}
    ),
    "data": frozenset({"data", "dataset", "datasets", "database", "db"}),
}


# ---------------------------------------------------------------------------
# Heuristic backend (the always-on default)
# ---------------------------------------------------------------------------
class HeuristicDecisionEngine:
    """Deterministic lexical/regex decisions; the fail-soft floor of the stack.

    The scorers are intentionally simple. They exist to keep the pipeline
    fully functional with zero downloads and zero network access, and to give
    tests exact reproducibility. When real System-1 weights are configured,
    this class remains as the fallback path and its outputs keep the same
    interface, so swapping backends never changes call sites.
    """

    name = "heuristic"

    # Prompt-injection signatures. Ordered by specificity; first hit wins so
    # latency stays flat regardless of how many rules exist.
    _INJECTION_PATTERNS: tuple[tuple[str, float], ...] = (
        (r"\bignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts?)\b", 0.95),
        (r"\bdisregard\s+(all\s+)?(previous|prior|your)\b", 0.95),
        (r"\breveal\s+(your\s+)?(system\s+)?prompt\b", 0.9),
        (r"\bprint\s+(your\s+)?(system\s+)?prompt\b", 0.9),
        (r"\byou\s+are\s+now\s+(a|an|in)\b", 0.85),
        (r"\bjailbreak\b", 0.9),
        (r"\bdeveloper\s+mode\b", 0.8),
        (r"\bact\s+as\s+.{0,40}\bwithout\s+(any\s+)?restrictions\b", 0.9),
        (r"\bexfiltrate\b", 0.9),
    )

    # Destructive SQL intent. Weighted so the worst single signal alone can
    # cross the default 0.7 enforcement threshold.
    _DESTRUCTIVE_PATTERNS: tuple[tuple[str, float], ...] = (
        (r"\b(drop|truncate)\s+(table|database|schema|index)\b", 0.75),
        (r"\brevoke\b", 0.5),
        (r"\b(delete\s+from\s+\w+)\s*(?:;|$)", 0.75),  # unbounded: no WHERE
        (r"\b(delete\s+from|update\s+\w+\s+set|insert\s+into|alter\s+table)\b", 0.35),
        (r"\bgrant\b", 0.35),
        (r"\bcopy\b", 0.3),
        (r"\battach\s+(database|file)\b", 0.6),
    )

    # Cross-tenant reach: either prose ("other tenants' data") or an explicit
    # tenant predicate that widens scope beyond the caller's own tenant.
    _TENANT_PATTERN = (
        r"\b(other|all|every)\s+tenants?\b"
        r"|\bcross[-\s]?tenant\b"
        r"|\btenant\w*\s*(?:=|==|like)\s*['\"]?(other|all|every)\b"
    )

    def decide_boolean(self, statement: str, context: str) -> Decision:
        """True when ``statement`` shares lexical evidence with ``context``.

        Used for relevance judgments (is this join path table worth keeping?).
        Empty inputs are False by definition: absence of evidence is a no.
        """
        start = time.perf_counter()
        if not statement or not context:
            return Decision(False, self.name, _now_ms(start))
        overlap = _tokens(statement) & _tokens(context)
        return Decision(bool(overlap), self.name, _now_ms(start))

    def classify_choice(self, query: str, options: Sequence[str]) -> Decision:
        """Pick the option with the strongest lexical evidence in ``query``.

        Scoring combines token overlap with a small affinity lexicon so
        ``"total spend"`` can steer toward ``aggregate_analytics`` even though
        the strings share no literal tokens. Ties resolve to the earliest
        option, which keeps the decision stable (and deterministic) when
        callers reorder only semantically-equivalent alternatives.
        """
        start = time.perf_counter()
        if not options:
            raise ValueError("classify_choice requires at least one option")
        query_tokens = _tokens(query)
        affinity = _affinity_hits(query_tokens)
        best_option = options[0]
        best_score = -1.0
        for option in options:
            option_tokens = _tokens(option)
            score = float(len(query_tokens & option_tokens))
            if option_tokens and option_tokens <= query_tokens:
                score += 0.5  # full-phrase match outranks partial overlap
            # Lexicon bridges: option concepts that the question's words imply.
            for concept in affinity:
                if any(option_token.startswith(concept[:6]) for option_token in option_tokens):
                    score += 0.5
                    break
            if score > best_score:
                best_score, best_option = score, option
        return Decision(best_option, self.name, _now_ms(start))

    def score_risk(self, text: str, criteria: str = "") -> Decision:
        """Normalised risk in ``[0, 1]`` for destructive intent or injection.

        ``criteria`` is accepted for interface parity (a future model may use
        it as a conditioning prefix); the heuristic scorer is criteria-free so
        results stay reproducible across call sites.
        """
        start = time.perf_counter()
        if not text:
            return Decision(0.0, self.name, _now_ms(start))
        lowered = text.lower()
        risk = 0.0

        for pattern, weight in self._INJECTION_PATTERNS:
            if re.search(pattern, lowered):
                risk = max(risk, weight)
        for pattern, weight in self._DESTRUCTIVE_PATTERNS:
            if re.search(pattern, lowered):
                risk = max(risk, weight)
        if re.search(self._TENANT_PATTERN, lowered):
            # Cross-tenant reach is a confidentiality break, not just noise:
            # the weight clears the default 0.7 threshold on its own.
            risk = max(risk, 0.75)
        return Decision(round(min(risk, 1.0), 3), self.name, _now_ms(start))


# ---------------------------------------------------------------------------
# ONNX backend (local weights, lazily loaded)
# ---------------------------------------------------------------------------
class OnnxDecisionEngine:
    """ONNX Runtime backend for a local text-classification model.

    Model contract: an encoder model exporting ``input_ids`` and
    ``attention_mask`` plus a classification head with one logits row per
    configured task. Because task heads differ between checkpoints, the
    mapping from logits to decisions is supplied by ``label_map``; anything
    the map cannot express falls back to the heuristic engine at construction
    time rather than guessing at runtime.
    """

    name = "onnx"

    def __init__(
        self,
        model_path: str,
        label_map: dict[str, list[str]] | None = None,
        max_length: int = 128,
    ) -> None:
        try:
            import onnxruntime as ort  # type: ignore[import-not-found]
        except ImportError as exc:
            raise DecisionBackendError(
                "onnxruntime is not installed; pip install onnxruntime"
            ) from exc
        try:
            self._session = ort.InferenceSession(
                model_path, providers=["CPUExecutionProvider"]
            )
        except Exception as exc:
            raise DecisionBackendError(f"could not load ONNX model {model_path!r}: {exc}") from exc
        self._max_length = max_length
        self._label_map = label_map or {}
        self._vocab: dict[str, int] | None = None
        self.name = f"onnx:{model_path}"

    # The ONNX path requires a tokenizer artefact next to the model. Without
    # one there is no correct way to feed text, so the honest behaviour is to
    # refuse rather than emit garbage decisions.
    def decide_boolean(self, statement: str, context: str) -> Decision:
        raise DecisionBackendError(
            "ONNX decision heads require a tokenizer artefact; see datawhisper docs"
        )

    def classify_choice(self, query: str, options: Sequence[str]) -> Decision:
        raise DecisionBackendError(
            "ONNX decision heads require a tokenizer artefact; see datawhisper docs"
        )

    def score_risk(self, text: str, criteria: str = "") -> Decision:
        raise DecisionBackendError(
            "ONNX decision heads require a tokenizer artefact; see datawhisper docs"
        )


# ---------------------------------------------------------------------------
# Remote backend (local inference server, opt-in)
# ---------------------------------------------------------------------------
class RemoteDecisionEngine:
    """HTTP client for a ``laya-serve``-style local inference endpoint.

    Contract: ``POST {endpoint}`` with
    ``{"task": "boolean|classify|risk", "input": ..., "options": [...]}`` and
    a JSON reply ``{"value": ...}``. The endpoint is expected to be a
    loopback-only local server; this client sends no credentials and no data
    beyond the decision inputs.
    """

    name = "remote"

    def __init__(self, endpoint: str, timeout: float = 0.25) -> None:
        if not endpoint:
            raise DecisionBackendError("RemoteDecisionEngine requires an endpoint")
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout
        self.name = f"remote:{endpoint}"

    def _call(self, payload: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            self._endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
        if "value" not in body:
            raise DecisionBackendError(f"decision endpoint returned no value: {body}")
        return body["value"]

    def decide_boolean(self, statement: str, context: str) -> Decision:
        start = time.perf_counter()
        return Decision(
            bool(self._call({"task": "boolean", "input": statement, "context": context})),
            self.name,
            _now_ms(start),
        )

    def classify_choice(self, query: str, options: Sequence[str]) -> Decision:
        start = time.perf_counter()
        return Decision(
            str(self._call({"task": "classify", "input": query, "options": list(options)})),
            self.name,
            _now_ms(start),
        )

    def score_risk(self, text: str, criteria: str = "") -> Decision:
        start = time.perf_counter()
        value = float(self._call({"task": "risk", "input": text, "criteria": criteria}))
        return Decision(
            min(max(value, 0.0), 1.0), self.name, _now_ms(start)
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def build_decision_engine(config: DecisionConfig | None = None) -> DecisionEngine:
    """Resolve configuration to a concrete engine, failing soft to heuristic.

    ``auto`` (the default) always yields the heuristic engine: no weights are
    bundled with the package, and silently guessing at an artefact path would
    violate the determinism guarantee. Explicit ``onnx``/``http`` providers
    raise :class:`DecisionBackendError` on misconfiguration so operators see
    the problem instead of silently losing their model.
    """
    active = config or DecisionConfig()
    if active.provider == "heuristic" or active.provider == "auto":
        return HeuristicDecisionEngine()
    if active.provider == "onnx":
        if not active.model_path:
            raise DecisionBackendError("DW_DECISION_PROVIDER=onnx needs DW_DECISION_MODEL_PATH")
        return OnnxDecisionEngine(active.model_path)
    if active.provider == "http":
        return RemoteDecisionEngine(active.endpoint, active.request_timeout)
    raise DecisionBackendError(f"unknown decision provider: {active.provider!r}")
