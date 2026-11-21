# Changelog

## [1.5.5] - 2026-11-21

### Fixed & Hardened

- **CI Linter Compliance (Ruff SIM102)**: Resolved nested conditional statement in `detect_cartesian_joins` (`security.py`), satisfying newer Ruff linter rules across multi-python CI matrix environments.
- **Differential Privacy Lower-Bound Protection**: Protected naturally non-negative count metrics against unbounded negative Laplace noise injection in `_apply_laplace_noise` (`privacy.py`) when no explicit `clamp_min` is specified.
- **PostgreSQL Identifier Length Bounds**: Capped auto-generated index names to 63 characters in `IndexAdvisor.advise()` (`advisor.py`) to prevent identifier truncation and collisions under PostgreSQL's 64-byte `NAMEDATALEN` limit.

## [1.5.4] - 2026-11-21

### Added

- **Native Semantic Layer & Data Contracts**: Introduced `SemanticCatalog`, `MetricDefinition`, `DimensionDefinition`, and `TableContract` with declarative validation in `verify_table_contract` and CLI command `datawhisper contract` to govern business metrics and prevent model hallucinations.
- **Differential Privacy ($\epsilon$-$\delta$ / Laplace Mechanism)**: Added `mode="differential_privacy"` with calibrated Laplace noise injection on analytical numeric metrics and aggregations to provide mathematical privacy guarantees against reconstruction and differencing attacks.
- **Hybrid Schema Pruning (BM25 + Dense Semantic Hash Projection)**: Augmented sparse BM25 retrieval with a 128-dimensional dense semantic hash projection capturing subword morphology and semantic synonymy with zero external dependencies and sub-millisecond execution.
- **Automated Index Advisor & Cost Optimizer**: Added AST-driven index recommendation engine (`IndexAdvisor`, `datawhisper advise`) proposing single and composite covering indexes with estimated cost reduction percentages.
- **Synthetic Mock Data Generator**: Implemented `SyntheticDataGenerator` to generate structurally coherent, referentially integral test datasets directly from `CatalogSnapshot` for air-gapped CI test suites.

### Fixed & Hardened

- **Deterministic ONNX Tokenization**: Replaced process-randomized string hashing with CRC32 deterministic token ID projection (`_deterministic_token_id`), guaranteeing invariant determinism across processes and environments.
- **Unified Policy Baseline**: Consolidated prompt injection and destructive SQL patterns into shared canonical structures across heuristic and neural inference drivers, guaranteeing consistent risk verdicts.
- **Transparent Neural Telemetry**: Recorded explicit fallback status in decision telemetry when neural inference encounters shape or model faults instead of silently degrading.
- **ExecutionEngine Lifecycle Safety**: Guarded `__del__` and `close()` with `hasattr` and exception suppression to prevent noisy destructor errors during interpreter teardown.
- **Sanitized Blocked Functions**: Removed non-standard function pseudo-names from blocked AST lists.

## [1.4.3] - 2026-11-01

### Fixed

- **DuckDB Session Cleanup**: Ensured DuckDB connection cursors and in-memory tables are strictly closed on session garbage collection or engine disposal via `__del__` and context manager support to prevent connection leaks during repetitive CLI runs.

### Added

- **HMAC Pseudonymization Session Salt**: Added support for `DW_PII_HMAC_SALT` environment variable with secure session fallback to ensure deterministic tokens remain stable across sub-processes during the same session while avoiding cross-run correlation when unconfigured.
- **Schema Graph Path Caching**: Cached computed Dijkstra join paths for frequent table pairs during long-running sessions to shave off recurring traversal latency.

## [1.4.2] - 2026-11-01

### Added

- **System-1 ONNX & Laya Integration**: Implemented `LayaOnnxDriver` and modular `OnnxDecisionEngine` inspired by `NandhaKishorM/laya`, achieving sub-20ms inference forward passes for risk scoring, intent classification, and boolean relevance judgments. Included deterministic fallback to the heuristic engine when weights or runtime are absent.
- **Semantic Pseudonymization (`mode="pseudonymize"`)**: Added persistent deterministic pseudonymization using HMAC-SHA256, generating uniform `ps_<hash>` tokens across the entire session to preserve cross-table referential integrity for `JOIN` and `GROUP BY` without exfiltrating raw values.
- **Advanced Schema Graph**:
  - Full support and path traversal for composite foreign keys across multiple columns in `JoinStep` and `SchemaGraph`.
  - Inferred implicit relationship resolution for standard entity abbreviations (`dept_id`, `cust_id`, `emp_id`, etc.) and matching non-generic primary keys.
  - Weighted Dijkstra pathfinding incorporating log table cardinality to avoid Cartesian explosions and choose optimal equijoin paths.
  - Dynamic Sub-graph Isolation: automatically isolates transitively connected k-hop subgraphs for enterprise catalogs (>50 tables) to prune prompt context and prevent hallucinations.
- **AST Three-Layer Sandbox False-Positive Fixes**:
  - Full validation and recognition for Common Table Expressions (`WITH ... AS` and `WITH RECURSIVE`), window functions (`OVER (PARTITION BY ... ORDER BY ...)`), and scalar subqueries in `WHERE` and `HAVING`.
  - Maintained fail-closed invariants: strictly blocking semicolons, injection comments, dangerous DuckDB table functions (`read_csv_raw`, `read_parquet_raw`), and shell execution.

## [1.3.9] - 2026-10-31

### Changed

- CI matrix now exercises Python 3.12, 3.14 and 3.15 (the newest runtime is
  non-blocking while runner support settles); the pytest suite fails on
  `DeprecationWarning`/`PendingDeprecationWarning` so runtime drift surfaces
  before releases.
- Minor internal housekeeping: env parsing consolidated to validated lookups,
  firewall scoring unified behind a single enforce path, stale no-op
  accumulation removed.

## [1.3.8] - 2026-10-22

### Security

- `RemoteDecisionEngine`: full SSRF boundary — http/https scheme allowlist,
  DNS resolution required to loopback/private addresses, link-local (cloud
  metadata) ranges denied at construction.
- PostgreSQL: statement timeout applied via bound-parameter `set_config`;
  SQLite catalog introspection via `pragma_table_info(?)` /
  `pragma_foreign_key_list(?)` bound parameters instead of interpolated
  identifiers.
- Test fixtures: synthetic credentials assembled from segments/character codes
  so no credential-shaped literal exists in source.

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
- Local-dialect SQL synthesis strategy for offline-running agents.

### Changed

- `schema_graph.py`, `security.py`, `privacy.py`, `agent.py` accept an optional
  decision engine with zero behavioural change when it is not configured.

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
