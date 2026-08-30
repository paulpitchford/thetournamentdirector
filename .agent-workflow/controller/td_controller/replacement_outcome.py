"""Exact outcome protocol for a future single-file replacement payload."""

from __future__ import annotations

from enum import Enum
from typing import Callable

from .attested_payload_runner import AttestedPayloadRunnerError
from .review_runtime import ProcessOutput

APPLIED_SENTINEL = b"text-replacement-ok\n"
INDETERMINATE_SENTINEL = b"text-replacement-indeterminate\n"
PRECONDITION_EXITS = frozenset({1, 51})
POST_MUTATION_EXIT = 52


class ReplacementOutcome(Enum):
    """Controller meaning assigned to exact bounded process output."""

    APPLIED = "applied"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"


class ReplacementOutcomeError(RuntimeError):
    """Raised when a replacement was not confirmed applied or rejected."""


class ReplacementIndeterminateError(ReplacementOutcomeError):
    """Raised when recovery must reconcile workspace bytes."""


def classify_replacement_output(output: ProcessOutput) -> ReplacementOutcome:
    """Classify only exact reviewed output shapes; ambiguity is indeterminate."""
    if not isinstance(output, ProcessOutput):
        return ReplacementOutcome.INDETERMINATE
    if (
        output.returncode == 0
        and output.stdout == APPLIED_SENTINEL
        and not output.stderr
    ):
        return ReplacementOutcome.APPLIED
    if (
        output.returncode in PRECONDITION_EXITS
        and not output.stdout
        and not output.stderr
    ):
        return ReplacementOutcome.REJECTED
    return ReplacementOutcome.INDETERMINATE


def require_applied(operation: Callable[[], ProcessOutput]) -> None:
    """Require exact applied evidence from one bounded runner call."""
    outcome = _run(operation)
    if outcome is ReplacementOutcome.APPLIED:
        return
    if outcome is ReplacementOutcome.REJECTED:
        raise ReplacementOutcomeError("replacement was rejected")
    raise ReplacementIndeterminateError(
        "replacement outcome requires reconciliation"
    )


def require_definitive_rejection(operation: Callable[[], ProcessOutput]) -> None:
    """Accept only the exact pre-mutation rejection shape as negative proof."""
    outcome = _run(operation)
    if outcome is ReplacementOutcome.REJECTED:
        return
    if outcome is ReplacementOutcome.APPLIED:
        raise ReplacementOutcomeError("unsafe replacement was accepted")
    raise ReplacementIndeterminateError(
        "replacement rejection proof was indeterminate"
    )


def _run(operation: Callable[[], ProcessOutput]) -> ReplacementOutcome:
    if not callable(operation):
        raise ReplacementOutcomeError("replacement operation is invalid")
    runner_failed = False
    try:
        output = operation()
    except AttestedPayloadRunnerError:
        runner_failed = True
        output = None
    if runner_failed:
        raise ReplacementIndeterminateError(
            "replacement outcome requires reconciliation"
        ) from None
    return classify_replacement_output(output)
