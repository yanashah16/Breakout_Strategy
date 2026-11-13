"""Logic engines. Importing this package registers every built-in engine."""
from desk.logic.base import BarContext, ExitRules, Logic, Plan, make, register  # noqa: F401
from desk.logic.patterns import adapter as _patterns  # noqa: F401  (registers "patterns")
