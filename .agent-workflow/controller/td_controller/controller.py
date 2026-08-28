"""Deterministic provider-run orchestration."""

from __future__ import annotations

from dataclasses import dataclass

from .pause import PauseSwitch
from .provider import Provider
from .state import RunLedger


class ControllerPausedError(RuntimeError):
    """Raised when dispatch is attempted while globally paused."""


@dataclass(frozen=True)
class RunResult:
    """Accepted provider output linked to its durable run record."""

    run_id: str
    summary: str
    session_id: str | None


class Controller:
    """Apply trusted pause/budget controls around one provider call."""

    def __init__(
        self,
        *,
        ledger: RunLedger,
        pause_switch: PauseSwitch,
        provider: Provider,
    ) -> None:
        self.ledger = ledger
        self.pause_switch = pause_switch
        self.provider = provider

    def run(self, *, task_id: str, role: str) -> RunResult:
        """Run one provider call only after deterministic admission."""
        pause_status = self.pause_switch.status()
        if pause_status.paused:
            raise ControllerPausedError(
                f"controller is paused: {pause_status.reason or 'unspecified'}"
            )

        run_id = self.ledger.reserve(task_id=task_id, role=role)
        try:
            pause_status = self.pause_switch.status()
            if pause_status.paused:
                raise ControllerPausedError(
                    f"controller paused during admission: "
                    f"{pause_status.reason or 'unspecified'}"
                )
            provider_result = self.provider.run(task_id=task_id, role=role)
        except Exception as exc:
            self.ledger.finish(run_id, status="FAILED", error=type(exc).__name__)
            raise

        self.ledger.finish(run_id, status="SUCCEEDED")
        return RunResult(
            run_id=run_id,
            summary=provider_result.summary,
            session_id=provider_result.session_id,
        )
