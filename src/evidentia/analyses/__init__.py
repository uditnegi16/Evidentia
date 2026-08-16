"""Analysis package.

Importing this package registers every core analysis. Registration is a side
effect of import, so `evidentia.analyses` must be imported before any config
that names an analysis is resolved.
"""

from evidentia.analyses import core  # noqa: F401  (import registers analyses)
from evidentia.analyses.registry import (
    AnalysisSpec,
    analysis,
    catalogue,
    get_spec,
    registered,
    run_analyses,
)

__all__ = [
    "AnalysisSpec",
    "analysis",
    "catalogue",
    "get_spec",
    "registered",
    "run_analyses",
]
