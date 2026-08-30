"""Outcome protocol for an exact Git ref creation after process launch."""

from __future__ import annotations

from enum import Enum

from .review_runtime import ProcessOutput


class RefObservation(Enum):
    """Trusted post-process observation of the exact direct ref."""

    ABSENT = "absent"
    EXACT = "exact"
    OTHER = "other"
    UNSAFE = "unsafe"


class RefCreationOutcome(Enum):
    """Authoritative interpretation of process and exact-ref evidence."""

    CREATED = "created"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"


class GitRefOutcomeError(RuntimeError):
    """Raised when exact-ref creation was not confirmed."""


class GitRefRejectedError(GitRefOutcomeError):
    """Raised when trusted observation confirms the ref is absent."""


class GitRefIndeterminateError(GitRefOutcomeError):
    """Raised when durable state must be reconciled before retry."""


def classify_ref_creation(
    *,
    launched: bool,
    process_output: ProcessOutput | None,
    runner_failed: bool,
    observation: RefObservation,
) -> RefCreationOutcome:
    """Classify creation only from process lifecycle plus post-run ref state."""
    if (
        not isinstance(launched, bool)
        or not isinstance(runner_failed, bool)
        or not isinstance(observation, RefObservation)
        or (process_output is not None and not isinstance(process_output, ProcessOutput))
        or (runner_failed and process_output is not None)
        or (not launched and (runner_failed or process_output is not None))
    ):
        return RefCreationOutcome.INDETERMINATE
    if not launched:
        return (
            RefCreationOutcome.REJECTED
            if observation is RefObservation.ABSENT
            else RefCreationOutcome.INDETERMINATE
        )
    if observation is RefObservation.EXACT:
        if (
            not runner_failed
            and process_output == ProcessOutput(0, b"", b"")
        ):
            return RefCreationOutcome.CREATED
        return RefCreationOutcome.INDETERMINATE
    if observation is not RefObservation.ABSENT:
        return RefCreationOutcome.INDETERMINATE
    if runner_failed:
        return RefCreationOutcome.REJECTED
    if process_output is not None and process_output.returncode != 0:
        return RefCreationOutcome.REJECTED
    return RefCreationOutcome.INDETERMINATE


def require_created(outcome: RefCreationOutcome) -> None:
    """Require exact normal creation; never turn ambiguity into clean failure."""
    if outcome is RefCreationOutcome.CREATED:
        return
    if outcome is RefCreationOutcome.REJECTED:
        raise GitRefRejectedError("exact ref was not created")
    raise GitRefIndeterminateError("exact ref requires reconciliation")
