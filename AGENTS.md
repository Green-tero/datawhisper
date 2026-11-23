# DataWhisper — Agent Instructions

This file is loaded into every agent session for the DataWhisper repository.
It encodes the project's stable decisions so future sessions start oriented
instead of re-deriving them.

## Project identity

- **Product:** DataWhisper — local-first agentic database copilot and CI/CD
  database governance engine. Public repo: `Green-tero/datawhisper`.
- **Release line:** 1.5.x (current release 1.5.9). Keep `pyproject.toml`,
  `datawhisper/__init__.py`, README badge and CHANGELOG in sync on every bump.
- **Dual role:** interactive local CLI/dashboard AND an autonomous PR auditor
  GitHub Action. Both share the same core.

## Architecture map

```
datawhisper/
  core/
    decision_engine.py   System-1: heuristic/ONNX/HTTP backends, <20ms decisions
    schema_graph.py      BM25 + 128-dim character-trigram feature hashing + FK graph + Dijkstra paths
    security.py          three-layer AST sandbox + EXPLAIN cost gate + SemanticFirewall
    privacy.py           PII detection + 6 redaction modes (including differential privacy)
    semantic.py          Semantic layer (governed metrics) + declarative Data Contracts
    advisor.py           AST-driven automated Index Advisor & Cost Optimizer
    synthetic.py         Mock data generator from CatalogSnapshot with referential integrity
    database.py          DatabaseBackend protocol: SQLite/DuckDB/Parquet/JSONL/Postgres
    agent.py             four-stage ReAct loop, zero LangChain; latency telemetry split
    config.py            pydantic policies (safety, cost, privacy, schema_graph, decision)
    models.py            shared contracts (catalog, plans, results, redaction, telemetry)
  integrations/
    github_bot.py        drift + PII exposure + plan regressions + sticky collapsible report
    migration_audit.py   offline static checks: lock risk, destructive drops, missing FK indexes
  visualization/dashboard.py  dark-mode Plotly, auto charts, plan view, redaction badges
  cli.py                 typer+rich: ask/inspect/explain/dashboard/audit; headless --ci-audit
```

## Core invariants (do not regress)

1. **Zero-trust model.** LLM output is untrusted: AST sandbox → cost gate →
   PII scrubber before data can leave. The model never sees raw PII.
2. **Fail-closed SQL.** Reject on any parse error or unclassified node, not on
   whitelist match. No semicolons/comments even in string literals (parser-
   differential bypass class).
3. **No network by default.** Decision engine defaults to `heuristic`; ONNX and
   `laya-serve`-style HTTP backends are opt-in. CI audit runs fully offline.
4. **Determinism.** Heuristic engine is the engine of record for tests/CI.
   Any configured non-heuristic backend must raise `DecisionBackendError` on
   misconfiguration rather than silently degrading.
5. **Privacy.** Scrub inside the engine, not at each consumer. Counts-only
   reports; `DW_PII_ALLOW_COLUMNS` is the single opt-out.
6. **Dual-speed firewall.** System-1 risk scoring before System-2 synthesis;
   threshold `DW_RISK_THRESHOLD` (default 0.7) raises `SecurityViolation`.

## Engine notes & gotchas

- DuckDB 2.x: use `connection.sql(...).to_arrow_table()` — `fetch_arrow_table`
  is gone. Never pair `execute()` and `sql()` on one query ("unfinished
  result"). `fetchone()` leaves the result unconsumed; use `fetchall()`.
- Parquet/JSONL backends register a lazy DuckDB view; `enable_external_access`
  stays ON (the view needs the file at scan time). Enforcement lives in the
  AST layer, not DuckDB config.
- `exp.Func` (not `exp.Function`) in sqlglot; dangerous DuckDB table functions
  parse to typed nodes (`read_csv` → `ReadCSV`), so block by snake-cased class
  name too.
- Cartesians are detected by AST connectivity (USING/NATURAL joins are
  connected, comma joins without predicates are not).
- Only a ROOT-level LIMIT bounds a statement for the cost gate.
- Correlation heatmaps must align rows (both columns numeric on the same row).

## Testing & quality gates (required before pushing)

```bash
poetry run pytest -v          # full suite (206 tests)
poetry run ruff check .       # clean
poetry run black --check .    # clean
poetry run mypy datawhisper   # strict, no issues
```

- Test with `pytest.importorskip` for `duckdb`/`pyarrow` optional deps.
- Push path: `git push` to `main` after tests pass, plus an annotated tag
  `v1.5.x`. Target environments: Python 3.13, 3.14, and 3.15.
- Credential: standard Git / GitHub CLI auth credentials; no tokens in files or configs.

## Latency budget (measured headroom)

- schema retrieval (50-table): ~1.3ms · AST validate: ~0.77ms · 10k-cell scrub:
  ~61ms · System-1 decision: ~23µs. System-2 is dominated by the local model.

## Memory pointers

- Docs live in `README.md`, `docs/README.fa.md`, `docs/configuration.md` and
  `CHANGELOG.md`. Long-lived decisions belong in this file rather than chat
  history.
- Start a fresh session for unrelated work; this file reloads the context.