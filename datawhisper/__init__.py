"""DataWhisper v2: zero-trust agentic data analysis and CI/CD governance.

The package keeps the agentic layer dependency-light (a pure-Python ReAct
loop), treats every model-produced statement as untrusted input (AST sandbox
plus a cost gate before execution), and scrubs sensitive values before any
result reaches an LLM context, a terminal or a pull-request comment.
"""

__version__ = "2.0.0"

__all__ = ["__version__"]
