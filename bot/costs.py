"""What one bot action cost: Apify runs and neural network requests."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class Costs:
    apify_usd: float = 0.0
    apify_runs: list[tuple[str, float]] = field(default_factory=list)
    llm_rub: float = 0.0
    llm_models: list[str] = field(default_factory=list)
    from_cache: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.apify_runs and not self.llm_models and not self.from_cache and not self.notes


_current: ContextVar[Costs | None] = ContextVar("costs", default=None)


def track() -> Costs:
    """Start counting for the current task; tasks it spawns add to the same object."""
    costs = Costs()
    _current.set(costs)
    return costs


def add_apify(actor_id: str, usd: float) -> None:
    costs = _current.get()
    if costs is not None:
        costs.apify_usd += usd
        costs.apify_runs.append((actor_id, usd))


def add_llm(model: str, rub: float | None) -> None:
    costs = _current.get()
    if costs is not None:
        costs.llm_rub += rub or 0.0
        if model not in costs.llm_models:
            costs.llm_models.append(model)


def mark_cached() -> None:
    costs = _current.get()
    if costs is not None:
        costs.from_cache = True


def add_note(text: str) -> None:
    costs = _current.get()
    if costs is not None and text not in costs.notes:
        costs.notes.append(text)
