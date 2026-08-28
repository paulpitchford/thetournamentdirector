"""Mandatory separation of local code/security and QA review agents."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Protocol

from .controller import Controller, RunResult
from .pause import PauseSwitch
from .provider import Provider
from .state import RunLedger


class ReviewSeparationError(RuntimeError):
    """Raised when required reviews do not use distinct fresh sessions."""


class ReviewProviderFactory(Protocol):
    """Create a fresh local provider process for one review role."""

    def create(self, *, role: str, source_read_only: bool) -> Provider:
        """Return a fresh provider with the requested source access mode."""
        ...


@dataclass(frozen=True)
class RequiredReviewEvidence:
    """Current-task evidence from both mandatory local review roles."""

    code_security: RunResult
    qa: RunResult


class ReviewCoordinator:
    """Run mandatory reviews in separate local providers and sessions."""

    def __init__(
        self,
        *,
        ledger: RunLedger,
        pause_switch: PauseSwitch,
        provider_factory: ReviewProviderFactory,
    ) -> None:
        self.ledger = ledger
        self.pause_switch = pause_switch
        self.provider_factory = provider_factory

    def run_required_reviews(
        self,
        *,
        task_id: str,
        implementation_session_id: str,
        other_excluded_session_ids: Collection[str] = (),
    ) -> RequiredReviewEvidence:
        """Run code/security and QA roles with independently created sessions."""
        if not implementation_session_id.strip():
            raise ReviewSeparationError("implementation session identity is required")
        excluded_session_ids = (
            implementation_session_id,
            *other_excluded_session_ids,
        )
        code_provider = self.provider_factory.create(
            role="code_review", source_read_only=True
        )
        qa_provider = self.provider_factory.create(role="qa_review", source_read_only=True)
        if code_provider is qa_provider:
            raise ReviewSeparationError("review roles reused one provider instance")

        code_result = Controller(
            ledger=self.ledger,
            pause_switch=self.pause_switch,
            provider=code_provider,
        ).run(task_id=task_id, role="code_review")
        self._require_fresh_session(
            code_result,
            role="code_review",
            excluded_session_ids=excluded_session_ids,
        )

        qa_result = Controller(
            ledger=self.ledger,
            pause_switch=self.pause_switch,
            provider=qa_provider,
        ).run(task_id=task_id, role="qa_review")
        self._require_fresh_session(
            qa_result,
            role="qa_review",
            excluded_session_ids=(
                *excluded_session_ids,
                code_result.session_id or "",
            ),
        )
        return RequiredReviewEvidence(code_security=code_result, qa=qa_result)

    @staticmethod
    def _require_fresh_session(
        result: RunResult,
        *,
        role: str,
        excluded_session_ids: Collection[str],
    ) -> None:
        session_id = result.session_id
        if session_id is None or not session_id.strip():
            raise ReviewSeparationError(f"{role} returned no session identity")
        if session_id in excluded_session_ids:
            raise ReviewSeparationError(f"{role} reused a forbidden session")
