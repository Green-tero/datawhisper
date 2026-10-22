"""DataWhisper 1.2: agentic data analysis and CI/CD database governance.

The package keeps the agentic layer dependency-light (a pure-Python ReAct
loop), treats every model-produced statement as untrusted input (AST sandbox
plus a cost gate before execution), and scrubs sensitive values before any
result reaches an LLM context, a terminal or a pull-request comment.
"""

__version__ = "1.3.8"

__all__ = ["__version__"]
