"""DataWhisper: local-first agentic tabular data engine with CI/CD auditing.

The package is deliberately dependency-light: the agentic engine is a pure
Python ReAct loop (no LangChain/LlamaIndex), the SQL sandbox is built on
``sqlglot`` ASTs, and heavyweight drivers (DuckDB) or SDKs (OpenAI, Gemini)
are optional extras that degrade gracefully when absent.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
