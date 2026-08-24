"""The engine: pure decision-making over domain types.

Imports `engine.domain` and nothing else. Emits commands; never executes them.
"""

from engine.core.decide import Decision, decide

__all__ = ["Decision", "decide"]
