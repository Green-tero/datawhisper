"""DataWhisper 1.3: agentic data analysis and CI/CD database governance.

The package keeps the agentic layer dependency-light (a pure-Python ReAct
loop with a local System-1 decision engine), treats every model-produced
statement as untrusted input (AST sandbox, cost gate and semantic firewall
before execution), and scrubs sensitive values before any result reaches an
LLM context, a terminal or a pull-request comment.
"""

__version__ = "1.3.9"

__all__ = ["__version__"]
