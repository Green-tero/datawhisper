# Changelog

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
