"""Auditable factor discovery and A-share research pipeline.

The package deliberately stops every newly mined factor at ``SHADOW_ONLY``;
individual data sources can impose a stricter research or engineering ceiling.
Broker execution remains a separate, explicitly approved boundary.
"""

from .expressions import Expression, evaluate_expression
from .pipeline import FactorFactory, FactorFactoryConfig, PipelineResult
from .qdata_input import (
    QDataInputError,
    QDataResearchResolver,
    ResolvedResearchInput,
    resolve_research_input,
)
from .schema import PanelData, panel_from_bars

__all__ = [
    "Expression",
    "FactorFactory",
    "FactorFactoryConfig",
    "PanelData",
    "PipelineResult",
    "QDataInputError",
    "QDataResearchResolver",
    "ResolvedResearchInput",
    "evaluate_expression",
    "panel_from_bars",
    "resolve_research_input",
]
