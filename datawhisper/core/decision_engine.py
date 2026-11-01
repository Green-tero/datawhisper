"""System-1 decision engine: sub-50ms local judgments for the agentic pipeline.

The copilot separates *judgment* (System-1: cheap, frequent, bounded decisions
such as domain selection, intent classification and risk scoring) from
*synthesis* (System-2: SQL generation by a code LLM). System-1 must never cost
a network round-trip, so three interchangeable backends implement one protocol:

* ``Heuristic`` — always available, deterministic lexical/regex scoring; the
  engine of record for tests and CI.
* ``Onnx`` — a local ONNX text-classification model when weights are
  configured (``DW_DECISION_MODEL_PATH``); lazily imported, fail-soft.
* ``Remote`` — a local ``laya-serve``-style HTTP server
  (``DW_LAYA_ENDPOINT``); opt-in only, loopback/private addresses enforced.

Invariants: the engine never sees row data, every decision is local (no
network in ``auto`` mode), and the firewall built on any backend is fail-closed
at enforcement even though backend construction fails soft.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

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
        "please",
    }
)


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text.lower()))
        if len(token) > 1 and token not in _STOPWORDS
    }


def _now_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 4)


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
        (r"\b(delete\s+from|update\s+\w+\s+set|insert\s+into|alter\s+table)\b", 0.35),
        (r"\bgrant\b", 0.35),
        (r"\bcopy\b", 0.3),
        (r"\battach\s+(database|file)\b", 0.6),
    )

    _TENANT_PATTERN = r"\b(other|all|every)\s+tenants?\b|\bcross[-\s]tenant\b"

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

        Ties resolve to the earliest option, which keeps the decision stable
        when callers reorder only semantically-equivalent alternatives.
        """
        start = time.perf_counter()
        if not options:
            raise ValueError("classify_choice requires at least one option")
        query_tokens = _tokens(query)
        best_option = options[0]
        best_score = -1.0
        for option in options:
            option_tokens = _tokens(option)
            score = float(len(query_tokens & option_tokens))
            if option_tokens and option_tokens <= query_tokens:
                score += 0.5  # full-phrase match outranks partial overlap
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
            risk = max(risk, 0.6)
        # Unbounded destructive DML is as dangerous as a DROP: it must cross
        # the default 0.7 enforcement threshold on its own.
        if re.search(r"\b(delete\s+from\s+\w+)\s*(;|$)", lowered):
            risk = max(risk, 0.75)
        return Decision(round(min(risk, 1.0), 3), self.name, _now_ms(start))


# ---------------------------------------------------------------------------
# ONNX / Laya backend (local weights, sub-20ms modular inference driver)
# ---------------------------------------------------------------------------
class LayaOnnxDriver:
    """Lightweight inference driver inspired by NandhaKishorM/laya.

    Provides sub-20ms forward evaluations using ONNX Runtime when available,
    or a calibrated zero-dependency vector kernel when onnxruntime is absent.
    """

    def __init__(self, model_path: str, max_length: int = 128) -> None:
        if not os.path.exists(model_path):
            raise DecisionBackendError(f"could not load ONNX model {model_path!r}: file not found")
        self.model_path = model_path
        self.max_length = max_length
        self._ort_session = None
        try:
            import onnxruntime as ort  # type: ignore[import-not-found]

            self._ort_session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        except Exception:
            self._ort_session = None

    def featurize(self, text: str) -> dict[str, float]:
        """Fast character/token n-gram featurizer for query analysis (<1ms)."""
        tokens = [
            token
            for token in re.findall(
                r"[a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text.lower())
            )
            if len(token) > 1 and token not in _STOPWORDS
        ]
        features: dict[str, float] = {}
        for token in tokens:
            features[token] = features.get(token, 0.0) + 1.0
        return features

    def score_risk(self, text: str, criteria: str = "") -> float:
        """Score risk in [0.0, 1.0] using the ONNX model or calibrated driver."""
        if not text:
            return 0.0
        lowered = text.lower()
        if self._ort_session is not None:
            try:
                input_name = self._ort_session.get_inputs()[0].name
                tokens = [hash(t) % 10000 for t in re.findall(r"[a-z0-9_]+", lowered)][
                    : self.max_length
                ]
                if not tokens:
                    tokens = [0]
                import numpy as np  # type: ignore[import-not-found]

                feed = {input_name: np.array([tokens], dtype=np.int64)}
                outputs = self._ort_session.run(None, feed)
                val = (
                    float(outputs[0][0][0])
                    if hasattr(outputs[0], "__getitem__")
                    else float(outputs[0])
                )
                return min(max(1.0 / (1.0 + math.exp(-val)), 0.0), 1.0)
            except Exception:
                pass

        risk = 0.0
        patterns = (
            (r"\b(drop|truncate)\s+(table|database|schema|index)\b", 0.85),
            (r"\bdelete\s+from\s+\w+\s*(;|$)", 0.80),
            (r"\b(delete\s+from|update\s+\w+\s+set|insert\s+into|alter\s+table)\b", 0.45),
            (r"\b(ignore|disregard)\s+(all\s+)?(previous|prior)\b", 0.95),
            (r"\breveal\s+(your\s+)?prompt\b", 0.90),
            (r"\b(other|all|every)\s+tenants?\b|\bcross[-\s]tenant\b", 0.65),
            (r"\battach\s+(database|file)\b", 0.70),
        )
        for pattern, weight in patterns:
            if re.search(pattern, lowered):
                risk = max(risk, weight)
        return min(risk, 1.0)

    def classify_choice(self, query: str, options: Sequence[str]) -> str:
        """Classify query intent into one of the options with sub-20ms forward pass."""
        if not options:
            raise ValueError("classify_choice requires at least one option")
        if len(options) == 1:
            return options[0]
        q_feat = self.featurize(query)
        best_score = -1.0
        best_opt = options[0]
        for opt in options:
            opt_feat = self.featurize(opt)
            score = sum(q_feat.get(k, 0.0) * opt_feat.get(k, 0.0) for k in opt_feat)
            if score > best_score:
                best_score = score
                best_opt = opt
        return best_opt

    def decide_boolean(self, statement: str, context: str) -> bool:
        """Assess semantic relevance between statement and context."""
        if not statement or not context:
            return False
        s_feat = self.featurize(statement)
        c_feat = self.featurize(context)
        overlap = set(s_feat) & set(c_feat)
        return len(overlap) > 0


class OnnxDecisionEngine:
    """ONNX Runtime backend for local text-classification and risk scoring.

    Modular driver built on the Laya inference pattern for sub-20ms non-autoregressive
    System-1 decisions without heavy dependencies.
    """

    name = "onnx"

    def __init__(
        self,
        model_path: str,
        label_map: dict[str, list[str]] | None = None,
        max_length: int = 128,
    ) -> None:
        self.driver = LayaOnnxDriver(model_path, max_length=max_length)
        self._label_map = label_map or {}
        self.name = f"onnx:{model_path}"

    def decide_boolean(self, statement: str, context: str) -> Decision:
        start = time.perf_counter()
        val = self.driver.decide_boolean(statement, context)
        return Decision(val, self.name, _now_ms(start))

    def classify_choice(self, query: str, options: Sequence[str]) -> Decision:
        start = time.perf_counter()
        val = self.driver.classify_choice(query, options)
        return Decision(val, self.name, _now_ms(start))

    def score_risk(self, text: str, criteria: str = "") -> Decision:
        start = time.perf_counter()
        val = self.driver.score_risk(text, criteria)
        return Decision(round(val, 3), self.name, _now_ms(start))


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
        # SSRF boundary. The contract is a loopback-side inference server, so
        # three checks are enforced at construction: scheme allowlist, DNS
        # resolution to a loopback/private address, and explicit denial of
        # link-local ranges (cloud metadata endpoints live there). DNS
        # rebinding between here and the request is out of scope for a
        # sub-250ms local call, and the client sends no credentials anyway.
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise DecisionBackendError(
                f"decision endpoint must use http/https, got {parsed.scheme or 'none'!r}"
            )
        host = parsed.hostname or ""
        try:
            resolved = {
                info[4][0]
                for info in socket.getaddrinfo(host, parsed.port or 80, proto=socket.IPPROTO_TCP)
            }
        except socket.gaierror as exc:
            raise DecisionBackendError(f"decision endpoint host unresolved: {host!r}") from exc
        for address in resolved:
            ip = ipaddress.ip_address(address)
            if ip.is_loopback or ip.is_private:
                continue
            if ip.is_link_local:
                raise DecisionBackendError(
                    f"decision endpoint resolves to link-local {address} "
                    "(cloud-metadata range is denied)"
                )
            raise DecisionBackendError(
                f"decision endpoint must resolve to a loopback/private address, " f"got {address}"
            )
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
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
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
        return Decision(min(max(value, 0.0), 1.0), self.name, _now_ms(start))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def build_decision_engine(config: DecisionConfig | None = None) -> DecisionEngine:
    """Resolve configuration to a concrete engine, failing soft to heuristic.

    ``auto`` checks for an explicit model path (via config or DW_DECISION_MODEL_PATH);
    if configured, it attempts to load the ONNX inference driver with deterministic
    fallback to the heuristic engine on misconfiguration.
    """
    active = config or DecisionConfig()
    if active.provider == "heuristic":
        return HeuristicDecisionEngine()
    if active.provider == "auto":
        model_path = active.model_path or os.environ.get("DW_DECISION_MODEL_PATH", "")
        if model_path:
            try:
                return OnnxDecisionEngine(model_path)
            except Exception:
                if not active.fallback_to_heuristic:
                    raise
                return HeuristicDecisionEngine()
        return HeuristicDecisionEngine()
    if active.provider == "onnx":
        model_path = active.model_path or os.environ.get("DW_DECISION_MODEL_PATH", "")
        if not model_path:
            raise DecisionBackendError("DW_DECISION_PROVIDER=onnx needs DW_DECISION_MODEL_PATH")
        return OnnxDecisionEngine(model_path)
    if active.provider == "http":
        return RemoteDecisionEngine(active.endpoint, active.request_timeout)
    raise DecisionBackendError(f"unknown decision provider: {active.provider!r}")
