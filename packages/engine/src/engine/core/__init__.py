"""The engine: pure decision-making over domain types.

Imports `engine.domain` and nothing else. Emits commands; never executes them.
"""

from engine.core.decide import Decision, decide
from engine.core.planning import PlanDecision, decide_plan

__all__ = ["Decision", "PlanDecision", "decide", "decide_plan"]
