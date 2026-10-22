# Changelog

## [1.3.8] - 2026-10-22

### Added

- `core/decision_engine.py`: System-1 decision engine with heuristic, ONNX and
  local-HTTP backends (sub-40ms local judgments; heuristic default keeps the
  pipeline fully offline).
- Dual-speed security firewall: fast semantic risk scoring ahead of the
  symbolic AST pass.
- Coarse-to-fine schema pruning via domain classification.
- Intent classification folded into the agent prompt.
- Latency telemetry split (`system_1_ms` / `system_2_ms` / `execution_ms`).
- IP-address detection and redaction in the privacy engine.
- Offline System-2: `local` LLM provider synthesises read-only SQL from
  deterministic templates, runs it through the sandbox, and answers from the
  measured observation — no API key, model download or network required.
- Static migration audit (`integrations/migration_audit.py`): lock risk,
  destructive drops and missing FK indexes per migration file, rendered in
  the governance report and contributing to the blocking verdict.

### Changed

- `schema_graph.py`, `security.py`, `privacy.py`, `agent.py` accept an optional
  decision engine with zero behavioural change when it is not configured.
- The audit CLI and PR bot now run the migration static checks on every
  `migrations/**` SQL file and fold the findings into the sticky report.

All notable changes are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [1.2.2] - 2026-10-21

### Fixed

- The cost gate treated a `LIMIT` inside a subquery as bounding the outer
  statement, so an unbounded outer scan could pass the budget check.
- `JOIN ... USING (...)` and `NATURAL JOIN` were misclassified as cartesian
  products, blocking valid queries.
- A planner that returned nodes without row estimates could bypass the scan
  budget; the gate now falls back to catalog row counts in that case.
- `ALTER TABLE ... ADD/DROP COLUMN` migrations were ignored by schema drift
  and PII exposure analysis.
- Parent-side primary keys were labelled as foreign keys in the pruned context.
- Correlation heatmaps paired values from different rows when a column had
  nulls, producing meaningless coefficients.
- Driver error messages could echo SQL literals, including PII, into the agent
  context, telemetry and terminal; the engine now scrubs them first.
- The GitHub contents API path was not URL-encoded, breaking files with spaces.

### Security

- `duckdb_secrets()`, DuckDB catalog helpers and `current_setting()` are now
  denied by the AST sandbox.
- The CLI scrubs the final SQL statement before printing it.

## [1.2.1] - 2026-10-21

### Fixed

- Escape model- and catalog-derived text in HTML reports.
- Neutralise HTML in PR insights so they cannot close the collapsed block.
- Report the real redaction-group count in the dashboard subtitle.
- Quote identifiers in the dashboard sample query.
- Remove an unused constructor flag and a dead import.

## [1.2.0] - 2026-10-21

Initial public release: graph-pruned schema retrieval, multi-engine Arrow
execution, cost gatekeeper, PII scrubbing, four-stage ReAct agent, pull-request
governance bot and dark-mode dashboard.
