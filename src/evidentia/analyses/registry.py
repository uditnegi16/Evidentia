"""Analysis registry.

Analyses register themselves by name. Report configuration then selects them by
name, which is what lets a second report type reuse the same analyses without
touching Python (D-001, D-008).

    @analysis("total_cases", "Total cases", unit="case")
    def total_cases(frame: CaseFrame) -> EvidenceItem: ...

    store = run_analyses(frame, ["total_cases", "serious_split"])

Registration is the only coupling between an analysis and the rest of the
system. Analyses do not know which report they serve, which section will quote
them, or that an LLM exists.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from evidentia.contracts import CaseFrame
from evidentia.evidence import CountUnit, EvidenceItem, EvidenceStore

AnalysisFn = Callable[..., EvidenceItem]


@dataclass(frozen=True)
class AnalysisSpec:
    key: str
    label: str
    unit: CountUnit
    fn: AnalysisFn
    defaults: dict[str, Any] = field(default_factory=dict)
    description: str = ""


_REGISTRY: dict[str, AnalysisSpec] = {}


def analysis(
    key: str,
    label: str,
    *,
    unit: CountUnit,
    defaults: dict[str, Any] | None = None,
    description: str = "",
) -> Callable[[AnalysisFn], AnalysisFn]:
    def decorate(fn: AnalysisFn) -> AnalysisFn:
        if key in _REGISTRY:
            raise ValueError(f"analysis already registered: {key}")
        _REGISTRY[key] = AnalysisSpec(
            key=key,
            label=label,
            unit=unit,
            fn=fn,
            defaults=dict(defaults or {}),
            description=description or (fn.__doc__ or "").strip().split("\n")[0],
        )
        return fn

    return decorate


def get_spec(key: str) -> AnalysisSpec:
    if key not in _REGISTRY:
        raise KeyError(
            f"unknown analysis '{key}'. registered: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[key]


def registered() -> list[str]:
    return sorted(_REGISTRY)


def catalogue() -> list[dict[str, Any]]:
    """Machine-readable inventory, used by the CLI and the README."""
    return [
        {
            "key": s.key,
            "label": s.label,
            "unit": s.unit,
            "params": s.defaults,
            "description": s.description,
        }
        for s in sorted(_REGISTRY.values(), key=lambda s: s.key)
    ]


def run_analyses(
    frame: CaseFrame,
    keys: list[str],
    params: dict[str, dict[str, Any]] | None = None,
) -> EvidenceStore:
    """Compute the requested analyses into a store.

    Unknown keys raise immediately rather than being skipped: a report config
    naming an analysis that does not exist is a configuration error, and
    discovering it at render time instead of load time is strictly worse.
    """
    params = params or {}
    store = EvidenceStore(
        period_start=frame.validation.period_start,
        period_end=frame.validation.period_end,
        source_sha256=frame.validation.source_sha256,
    )
    for key in dict.fromkeys(keys):
        spec = get_spec(key)
        kwargs = {**spec.defaults, **params.get(key, {})}
        store.add(spec.fn(frame, **kwargs))
    return store
