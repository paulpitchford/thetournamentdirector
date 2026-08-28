"""Provider boundary used by the deterministic controller."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProviderResult:
    """Minimal structured result returned by an agent provider."""

    summary: str
    session_id: str | None = None


class Provider(Protocol):
    """Interface implemented by bounded model-provider adapters."""

    def run(self, *, task_id: str, role: str) -> ProviderResult:
        """Run one role for one validated task."""
        ...


class FakeProvider:
    """Deterministic provider for controller and recovery tests."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def run(self, *, task_id: str, role: str) -> ProviderResult:
        """Record a call and return configured deterministic output."""
        self.calls.append((task_id, role))
        if self.error is not None:
            raise self.error
        return ProviderResult(summary=f"fake:{task_id}:{role}")
