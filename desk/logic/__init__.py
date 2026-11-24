"""Logic engines. Importing this package registers every built-in engine."""
from desk.logic.base import BarContext, ExitRules, Logic, Plan, make, register  # noqa: F401
from desk.logic.patterns import adapter as _patterns  # noqa: F401  (registers "patterns")
from desk.logic.patterns import lines as _lines  # noqa: F401  (registers "lines")
from desk.logic import flatzone as _flatzone  # noqa: F401,E402  (registers "flatzone")
from desk.logic import box as _box  # noqa: F401,E402  (registers "box")
