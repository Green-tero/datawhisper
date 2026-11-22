# DataWhisper Configuration Reference

All settings in DataWhisper can be configured via environment variables (`DW_*`) or passed directly to `DataWhisperConfig` in Python code.

---

## 1. Safety & AST Sandbox (`DW_SAFETY_*`, `DW_ALLOW_*`, `DW_DISALLOW_*`)

| Environment Variable | Type | Default | Description |
| -------------------- | ---- | ------- | ----------- |
| `DW_ALLOW_DDL` | bool | `false` | When true, permits DDL statements (`CREATE`, `ALTER`, `DROP`). Kept false for read-only querying. |
| `DW_ALLOW_DML` | bool | `false` | When true, permits DML mutations (`INSERT`, `UPDATE`, `DELETE`). |
| `DW_ALLOW_EXPORT` | bool | `false` | When true, allows statements that write out data (e.g. `COPY ... TO`, `EXPORT DATABASE`). |
| `DW_ALLOW_SYSTEM_TABLES` | bool | `false` | When true, allows querying system metadata tables (e.g., `sqlite_master`, `pg_catalog`, `information_schema`). |
| `DW_ALLOW_UNBOUNDED_QUERIES` | bool | `false` | When false, queries without an explicit top-level `LIMIT` will either be rewritten with a limit or rejected by the cost gate. |
| `DW_MAX_LIMIT` | int | `1000` | Upper bound for root `LIMIT`. Statements with limits exceeding this value are rejected. |
| `DW_DEFAULT_LIMIT` | int | `100` | Injected limit if a query omits `LIMIT` and pagination is enforced. |
| `DW_DISALLOWED_FUNCTIONS` | list[str] | `["load_extension", "system", "cmd", "shell", "read_csv", "read_parquet", "glob"]` | Blocked SQL function names to prevent arbitrary code execution or file reads. |
| `DW_ALLOWED_TABLES` | list[str] | `[]` (empty = all) | Explicit allowlist of accessible tables. If set, queries referencing any other table are rejected. |
| `DW_BLOCKED_TABLES` | list[str] | `[]` | Explicit denylist of inaccessible tables. |

---

## 2. Privacy & PII Scrubbing (`DW_PII_*`)

| Environment Variable | Type | Default | Description |
| -------------------- | ---- | ------- | ----------- |
| `DW_PII_MODE` | str | `mask` | PII redaction mode: `mask`, `hash`, `pseudonymize`, `tokenize`, `differential_privacy`, `none`. |
| `DW_PII_HMAC_SALT` | str | random | Salt key for persistent HMAC-SHA256 pseudonymization. Set to keep pseudonyms consistent across sessions. |
| `DW_PII_EPSILON` | float | `1.0` | Privacy parameter $\epsilon$ for differential privacy noise scale. Lower values provide higher privacy. |
| `DW_PII_DELTA` | float | `1e-5` | Relaxed differential privacy parameter $\delta$. |
| `DW_PII_ALLOW_COLUMNS` | list[str] | `[]` | Comma-separated list of column names exempt from automatic PII scrubbing. |

---

## 3. Cost & Execution Policy (`DW_MAX_*`, `DW_TIMEOUT_*`)

| Environment Variable | Type | Default | Description |
| -------------------- | ---- | ------- | ----------- |
| `DW_MAX_ESTIMATED_ROWS` | int | `500000` | Maximum estimated rows scanned allowed by the query planner before rejecting execution. |
| `DW_REJECT_CARTESIAN` | bool | `true` | Rejects unconstrained Cartesian joins (`CROSS JOIN` or multiple `FROM` tables without join predicates). |
| `DW_EXECUTION_TIMEOUT_SECONDS` | float | `30.0` | Hard query execution timeout before interrupting the driver. |
| `DW_MAX_REPAIR_ATTEMPTS` | int | `3` | Maximum autonomous SQL self-correction attempts during the ReAct loop. |

---

## 4. Schema Graph & Hybrid Retrieval (`DW_GRAPH_*`, `DW_MAX_DOMAINS`)

| Environment Variable | Type | Default | Description |
| -------------------- | ---- | ------- | ----------- |
| `DW_GRAPH_HYBRID_DENSE` | bool | `true` | Enables 128-dimensional character-trigram hash projection alongside sparse BM25 scoring. |
| `DW_DOMAIN_PRUNING` | bool | `true` | Groups large schemas into business domains before ranking tables to keep context concise. |
| `DW_MAX_DOMAINS` | int | `4` | Maximum number of candidate domains retained during schema pruning. |
| `DW_TOKEN_BUDGET` | int | `4000` | Token budget ceiling for serializing schema tables and foreign key paths into LLM prompts. |

---

## 5. System-1 Decision Engine & Firewall (`DW_DECISION_*`, `DW_RISK_*`)

| Environment Variable | Type | Default | Description |
| -------------------- | ---- | ------- | ----------- |
| `DW_DECISION_ENGINE` | str | `heuristic` | Engine backend: `heuristic` (embedded rules), `onnx` (local model), `http` (sidecar server). |
| `DW_RISK_THRESHOLD` | float | `0.70` | Safety score threshold $[0.0, 1.0]$. Queries scoring at or above this threshold trigger `SecurityViolation`. |
| `DW_ONNX_MODEL_PATH` | str | `None` | Path to local ONNX model file when using the ONNX decision driver. |
| `DW_DECISION_ENDPOINT` | str | `None` | URL to remote decision microservice when using HTTP driver. |

---

## 6. Language Model Provider (`DW_LLM_*`, `DW_MODEL`)

| Environment Variable | Type | Default | Description |
| -------------------- | ---- | ------- | ----------- |
| `DW_LLM_PROVIDER` | str | `gemini` | LLM provider: `gemini`, `openai`, `mock`. |
| `DW_MODEL` | str | provider default | Model ID (e.g., `gemini-2.5-flash` or `gpt-4o-mini`). |
| `DW_TEMPERATURE` | float | `0.0` | Sampling temperature for SQL generation (0.0 recommended for deterministic queries). |
| `GEMINI_API_KEY` | str | `None` | API key for Google Gemini (`google-genai` client). |
| `OPENAI_API_KEY` | str | `None` | API key for OpenAI API. |
