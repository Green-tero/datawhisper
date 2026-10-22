"""Zero-leakage PII detection and redaction.

Threat model
------------
Model output, terminal rendering and pull-request comments are three distinct
exfiltration channels. A single scrubbing point that every channel shares is
therefore safer than per-channel filtering, so the execution engine scrubs
batches as they leave the driver and every consumer receives only scrubbed
rows.

Two signals are combined:

* **Column semantics.** A column named ``email`` or ``card_number`` is treated
  as sensitive regardless of its values, because a single non-matching sample
  must not leak the column's contents.
* **Value patterns.** Free-text values are scanned for emails, Luhn-valid card
  numbers, SSNs, formatted phone numbers, provider API keys and JWTs, so
  secrets embedded in log lines or JSON blobs are caught too.

The report contains counts and column names only - never a detected value -
so telemetry can be published safely.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from datawhisper.core.config import PrivacyConfig
from datawhisper.core.decision_engine import DecisionEngine
from datawhisper.core.models import RedactionReport


class PiiCategory(StrEnum):
    """Sensitive data classes recognised by the scrubber."""

    EMAIL = "EMAIL"
    CREDIT_CARD = "CREDIT_CARD"
    SSN = "SSN"
    PHONE = "PHONE"
    API_KEY = "API_KEY"
    JWT = "JWT"
    IP_ADDRESS = "IP_ADDRESS"


class PiiMatch(BaseModel):
    """A detected sensitive span inside one value."""

    category: PiiCategory
    value: str
    start: int
    end: int


# ---------------------------------------------------------------------------
# Value detectors
# ---------------------------------------------------------------------------
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_CARD_CANDIDATE = re.compile(r"(?<![\d.])(?:\d[ -]?){12,19}\d(?![\d.])")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_PHONE = re.compile(
    r"(?<![\w.])"
    r"(?:\+\d{1,3}[\s.\-]?)?"
    r"(?:\(\d{2,4}\)[\s.\-]?|\d{2,4}[\s.\-])"
    r"\d{3,4}[\s.\-]?\d{3,4}"
    r"(?![\w.])"
)
_API_KEY = re.compile(
    r"(?:"
    r"\bsk-(?:live|test|proj)-[A-Za-z0-9_\-]{16,}"
    r"|\bsk-[A-Za-z0-9]{20,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{36,}\b"
    r"|\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"
    r"|\bAIza[0-9A-Za-z_\-]{35}\b"
    r")"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\b")
# Octet-bounded IPv4; the lookarounds keep dotted version strings out.
_IP = re.compile(
    r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?![\d.])"
)

#: Column names that imply sensitivity even when every sampled value looks
#: innocuous. Matching is token-based (``user_email`` matches ``email``).
_SENSITIVE_COLUMN_TOKENS: dict[str, PiiCategory] = {
    "email": PiiCategory.EMAIL,
    "mail": PiiCategory.EMAIL,
    "ssn": PiiCategory.SSN,
    "social_security": PiiCategory.SSN,
    "phone": PiiCategory.PHONE,
    "mobile": PiiCategory.PHONE,
    "tel": PiiCategory.PHONE,
    "telephone": PiiCategory.PHONE,
    "credit_card": PiiCategory.CREDIT_CARD,
    "card_number": PiiCategory.CREDIT_CARD,
    "ccn": PiiCategory.CREDIT_CARD,
    "pan": PiiCategory.CREDIT_CARD,
    "cvv": PiiCategory.CREDIT_CARD,
    "cvc": PiiCategory.CREDIT_CARD,
    "iban": PiiCategory.CREDIT_CARD,
    "api_key": PiiCategory.API_KEY,
    "apikey": PiiCategory.API_KEY,
    "access_key": PiiCategory.API_KEY,
    "private_key": PiiCategory.API_KEY,
    "secret": PiiCategory.API_KEY,
    "token": PiiCategory.API_KEY,
    "password": PiiCategory.API_KEY,
    "passwd": PiiCategory.API_KEY,
    "jwt": PiiCategory.JWT,
    "ip": PiiCategory.IP_ADDRESS,
    "ip_address": PiiCategory.IP_ADDRESS,
    "inet": PiiCategory.IP_ADDRESS,
    "host_ip": PiiCategory.IP_ADDRESS,
}


def _normalize_identifier(name: str) -> str:
    """Lowercase a column name and split camelCase into underscore tokens."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return re.sub(r"[^a-z0-9]+", "_", spaced.lower()).strip("_")


def column_category(name: str) -> PiiCategory | None:
    """Infer sensitivity from a column name, preferring the longest token match."""
    normalized = _normalize_identifier(name)
    tokens = set(normalized.split("_"))
    best: tuple[int, PiiCategory] | None = None
    for keyword, category in _SENSITIVE_COLUMN_TOKENS.items():
        hit = keyword in normalized if "_" in keyword else keyword in tokens
        if hit and (best is None or len(keyword) > best[0]):
            best = (len(keyword), category)
    return best[1] if best else None


def luhn_valid(digits: str) -> bool:
    """Validate a card number with the Luhn checksum (rejects random digit runs)."""
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _card_match(candidate: str) -> bool:
    digits = re.sub(r"\D", "", candidate)
    # Reject unformatted 13-16 digit numeric identifiers that fail Luhn; the
    # checksum is the only cheap signal that distinguishes cards from IDs.
    return luhn_valid(digits)


def detect_pii(value: str) -> list[PiiMatch]:
    """Return non-overlapping sensitive spans in ``value``, left to right.

    Detection order is by specificity: cryptographic tokens first, then cards
    (checksum-validated), then structured identifiers. Overlaps are resolved in
    favour of the earlier, more specific detector.
    """
    if not value or len(value) < 4:
        return []

    candidates: list[PiiMatch] = []
    for pattern, category in (
        (_JWT, PiiCategory.JWT),
        (_API_KEY, PiiCategory.API_KEY),
        (_SSN, PiiCategory.SSN),
        (_EMAIL, PiiCategory.EMAIL),
    ):
        for match in pattern.finditer(value):
            candidates.append(
                PiiMatch(
                    category=category,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                )
            )

    for match in _CARD_CANDIDATE.finditer(value):
        if _card_match(match.group(0)):
            candidates.append(
                PiiMatch(
                    category=PiiCategory.CREDIT_CARD,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                )
            )

    for match in _PHONE.finditer(value):
        candidates.append(
            PiiMatch(
                category=PiiCategory.PHONE,
                value=match.group(0),
                start=match.start(),
                end=match.end(),
            )
        )

    for match in _IP.finditer(value):
        candidates.append(
            PiiMatch(
                category=PiiCategory.IP_ADDRESS,
                value=match.group(0),
                start=match.start(),
                end=match.end(),
            )
        )

    candidates.sort(key=lambda m: (m.start, -(m.end - m.start)))
    accepted: list[PiiMatch] = []
    cursor = -1
    for candidate in candidates:
        if candidate.start >= cursor:
            accepted.append(candidate)
            cursor = candidate.end
    return accepted


def contains_pii(value: str) -> bool:
    """Convenience predicate for tests and CI guards."""
    return bool(detect_pii(value)) or column_category(value) is not None


# ---------------------------------------------------------------------------
# Scrubber
# ---------------------------------------------------------------------------
class PrivacyScrubber:
    """Deterministic, channel-agnostic redaction of tabular batches.

    Invariants:
    * Output rows never contain a detected raw value.
    * The returned report contains counts only.
    * ``hash`` mode is stable across processes for a fixed salt, which keeps
      pseudonyms comparable between CI runs.
    """

    def __init__(
        self,
        config: PrivacyConfig | None = None,
        decision: DecisionEngine | None = None,
    ) -> None:
        self.config = config or PrivacyConfig()
        self._allow = {name.lower() for name in self.config.column_allowlist}
        self._tokens: dict[tuple[PiiCategory, str], str] = {}
        self._counters: dict[PiiCategory, int] = {}
        # Optional System-1 classifier: identifiers the exact token map does
        # not know are judged locally by the decision engine. The static map
        # always wins - it is exact and free - so the engine only settles
        # unfamiliar names, and its verdicts are cached per column.
        self._decision = decision
        self._classification_cache: dict[str, PiiCategory | None] = {}

    def classify_column(self, name: str) -> PiiCategory | None:
        """Classify a column name: exact map first, then the decision engine.

        The heuristic engine answers "non-sensitive" when a name shares no
        evidence with any known category, which keeps false positives at zero
        while still catching disguised names like ``contact_info`` once real
        System-1 weights are configured.
        """
        exact = column_category(name)
        if exact is not None or name in self._classification_cache:
            return self._classification_cache.get(name, exact)
        verdict: PiiCategory | None = None
        if self._decision is not None:
            options = [category.value for category in PiiCategory] + ["NON_SENSITIVE"]
            choice = self._decision.classify_choice(name, options)
            if choice.value != "NON_SENSITIVE":
                try:
                    verdict = PiiCategory(str(choice.value))
                except ValueError:
                    verdict = None
        self._classification_cache[name] = verdict
        return verdict

    def scrub(
        self, columns: Sequence[str], rows: Iterable[Sequence[Any]]
    ) -> tuple[list[list[Any]], RedactionReport]:
        """Redact a batch, returning new rows and a counts-only report."""
        if not self.config.enabled:
            return [list(row) for row in rows], RedactionReport(mode="disabled")

        column_categories: list[PiiCategory | None] = []
        column_allowed: list[bool] = []
        for column in columns:
            allowed = column.lower() in self._allow
            column_allowed.append(allowed)
            column_categories.append(None if allowed else self.classify_column(column))

        scrubbed: list[list[Any]] = []
        by_category: dict[str, int] = {}
        affected_columns: set[str] = set()
        rows_affected = 0
        total = 0

        for row in rows:
            out_row: list[Any] = []
            row_hit = False
            for index, cell in enumerate(row):
                # An allowlisted column is a deliberate disclosure decision:
                # skip both name-based and value-based scrubbing for it.
                if index < len(column_allowed) and column_allowed[index]:
                    out_row.append(cell)
                    continue
                category = column_categories[index] if index < len(column_categories) else None
                if cell is None:
                    out_row.append(None)
                    continue
                if category is not None:
                    replacement = self._render(category, str(cell))
                    out_row.append(replacement)
                    by_category[category.value] = by_category.get(category.value, 0) + 1
                    affected_columns.add(columns[index])
                    total += 1
                    row_hit = True
                    continue
                if isinstance(cell, str):
                    scrubbed_value, hits = self._scrub_text(cell)
                    out_row.append(scrubbed_value)
                    for hit_category in hits:
                        by_category[hit_category.value] = by_category.get(hit_category.value, 0) + 1
                        total += 1
                        row_hit = True
                    if hits:
                        affected_columns.add(columns[index])
                    continue
                out_row.append(cell)
            if row_hit:
                rows_affected += 1
            scrubbed.append(out_row)

        report = RedactionReport(
            total=total,
            by_category=by_category,
            columns=sorted(affected_columns),
            rows_affected=rows_affected,
            mode=self.config.mode,
        )
        return scrubbed, report

    def scrub_text(self, text: str) -> tuple[str, RedactionReport]:
        """Scrub a free-form string (used for errors and report fragments)."""
        scrubbed, hits = self._scrub_text(text)
        by_category: dict[str, int] = {}
        for category in hits:
            by_category[category.value] = by_category.get(category.value, 0) + 1
        return scrubbed, RedactionReport(
            total=len(hits),
            by_category=by_category,
            rows_affected=1 if hits else 0,
            mode=self.config.mode,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _scrub_text(self, text: str) -> tuple[str, list[PiiCategory]]:
        matches = detect_pii(text)
        if not matches:
            return text, []
        pieces: list[str] = []
        cursor = 0
        categories: list[PiiCategory] = []
        for match in matches:
            pieces.append(text[cursor : match.start])
            pieces.append(self._render(match.category, match.value))
            cursor = match.end
            categories.append(match.category)
        pieces.append(text[cursor:])
        return "".join(pieces), categories

    def _render(self, category: PiiCategory, value: str) -> str:
        mode = self.config.mode
        if mode == "hash":
            digest = hmac.new(
                self.config.salt.encode("utf-8"),
                f"{category.value}:{value}".encode(),
                hashlib.sha256,
            ).hexdigest()[: self.config.hash_length]
            return f"{category.value.lower()}_{digest}"
        if mode == "tokenize":
            key = (category, value)
            token = self._tokens.get(key)
            if token is None:
                self._counters[category] = self._counters.get(category, 0) + 1
                token = f"<{category.value}:{self._counters[category]}>"
                self._tokens[key] = token
            return token
        if mode == "mask":
            return _mask(category, value)
        return f"[REDACTED:{category.value}]"


def _mask(category: PiiCategory, value: str) -> str:
    """Format-preserving mask that keeps just enough for human verification."""
    if category is PiiCategory.IP_ADDRESS:
        return "0.0.0.0"
    digits = re.sub(r"\D", "", value)
    if category is PiiCategory.EMAIL and "@" in value:
        local, _, domain = value.partition("@")
        head = local[:1] if local else ""
        return f"{head}***@{domain}"
    if category is PiiCategory.CREDIT_CARD and len(digits) >= 4:
        return f"**** **** **** {digits[-4:]}"
    if category is PiiCategory.SSN and len(digits) >= 4:
        return f"***-**-{digits[-4:]}"
    if category is PiiCategory.PHONE and len(digits) >= 4:
        return f"***-***-{digits[-4:]}"
    if category in {PiiCategory.API_KEY, PiiCategory.JWT}:
        return f"{value[:4]}...{value[-2:]}" if len(value) > 8 else f"{value[:2]}***"
    return f"{value[:2]}***{value[-2:]}" if len(value) > 6 else "***"
