"""Exact dependency selection for the future mutation coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .attested_text_replacement import AttestedTextReplacementApplier
from .text_mutation_broker import TextMutationBroker


class MutationDependencyError(ValueError):
    """Raised when a coordinator dependency is not callable as required."""


@dataclass(frozen=True)
class MutationDependencies:
    """Validated broker and applier identities."""

    broker: object
    applier: object


def build_mutation_dependencies(
    *,
    broker: object | None = None,
    applier: object | None = None,
    broker_factory: Callable[[], object] = TextMutationBroker,
    applier_factory: Callable[[], object] = AttestedTextReplacementApplier,
) -> MutationDependencies:
    """Use supplied dependencies exactly; construct defaults only for None."""
    if not callable(broker_factory) or not callable(applier_factory):
        raise MutationDependencyError("mutation dependency factory is invalid")
    selected_broker = broker if broker is not None else broker_factory()
    if not callable(getattr(selected_broker, "run", None)):
        raise MutationDependencyError("mutation broker is invalid")
    selected_applier = applier if applier is not None else applier_factory()
    if not callable(getattr(selected_applier, "apply", None)):
        raise MutationDependencyError("mutation applier is invalid")
    return MutationDependencies(selected_broker, selected_applier)
