# Contributing to DataWhisper

Thank you for your interest in contributing to DataWhisper!

DataWhisper is a local-first tabular data assistant and PR governance auditor with strict zero-trust invariants and high engineering standards.

---

## 1. Development Setup

### Prerequisites
- Python 3.13, 3.14, or 3.15
- [Poetry](https://python-poetry.org/)

### Clone & Install
```bash
git clone https://github.com/Green-tero/datawhisper.git
cd datawhisper

# Install dependencies with all extras
poetry install --with dev --extras "duckdb arrow postgres llm"

# Install pre-commit hooks
poetry run pre-commit install
```

---

## 2. Quality Gates

All contributions must pass our standard quality checks before being merged:

```bash
# 1. Run the full test suite (206 tests)
poetry run pytest -v

# 2. Run Ruff linting
poetry run ruff check .

# 3. Check code formatting with Black
poetry run black --check .

# 4. Run strict type checking with Mypy
poetry run mypy datawhisper
```

---

## 3. Architecture & Invariants

When contributing new features or bug fixes, keep these invariants in mind:

1. **Zero-Trust AST Sandbox:** Never execute raw SQL without passing it through `SemanticFirewall` and `ASTValidator`.
2. **Determinism:** Core behavior (such as heuristic decisions, schema graph traversal, and SQL validation) must be 100% deterministic and reproducible across operating systems.
3. **In-Engine Privacy:** PII detection and redaction must happen inside `privacy.py` before data is rendered or exported.
4. **No Heavy Agent Frameworks:** We avoid heavyweight frameworks (e.g. LangChain, LlamaIndex). The ReAct loop is implemented in pure, readable Python in `datawhisper/core/agent.py`.
5. **No Network by Default:** Tests must pass offline without external API keys or live database servers. Use SQLite and in-memory DuckDB for unit testing.

---

## 4. Pull Request Workflow

1. Fork the repository and create a feature branch (`git checkout -b feature/my-feature`).
2. Add comprehensive unit tests in `tests/` covering your changes.
3. Ensure all quality gates pass locally.
4. Submit a Pull Request targeting the `main` branch with a clear summary of your changes.
