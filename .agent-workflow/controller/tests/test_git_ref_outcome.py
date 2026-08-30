from __future__ import annotations

import unittest

from td_controller.git_ref_outcome import (
    GitRefIndeterminateError,
    GitRefOutcomeError,
    RefCreationOutcome,
    RefObservation,
    classify_ref_creation,
    require_created,
)
from td_controller.review_runtime import ProcessOutput


class GitRefOutcomeTests(unittest.TestCase):
    def test_exact_normal_success_is_created(self) -> None:
        outcome = classify_ref_creation(
            launched=True, process_output=ProcessOutput(0, b"", b""),
            runner_failed=False, observation=RefObservation.EXACT,
        )
        self.assertIs(outcome, RefCreationOutcome.CREATED)
        require_created(outcome)

    def test_prelaunch_or_completed_failure_requires_confirmed_absence(self) -> None:
        rejected = (
            classify_ref_creation(
                launched=False, process_output=None, runner_failed=False,
                observation=RefObservation.ABSENT,
            ),
            classify_ref_creation(
                launched=True, process_output=ProcessOutput(1, b"", b"detail"),
                runner_failed=False, observation=RefObservation.ABSENT,
            ),
            classify_ref_creation(
                launched=True, process_output=None, runner_failed=True,
                observation=RefObservation.ABSENT,
            ),
        )
        for outcome in rejected:
            with self.subTest(outcome=outcome):
                self.assertIs(outcome, RefCreationOutcome.REJECTED)
                with self.assertRaises(GitRefOutcomeError):
                    require_created(outcome)

    def test_committed_ref_plus_runner_or_output_ambiguity_is_indeterminate(self) -> None:
        ambiguous = (
            (None, True),
            (ProcessOutput(0, b"unexpected", b""), False),
            (ProcessOutput(0, b"", b"unexpected"), False),
            (ProcessOutput(1, b"", b""), False),
        )
        for output, failed in ambiguous:
            with self.subTest(output=output, failed=failed):
                outcome = classify_ref_creation(
                    launched=True, process_output=output, runner_failed=failed,
                    observation=RefObservation.EXACT,
                )
                self.assertIs(outcome, RefCreationOutcome.INDETERMINATE)
                with self.assertRaises(GitRefIndeterminateError):
                    require_created(outcome)

    def test_other_or_unsafe_ref_state_is_always_indeterminate(self) -> None:
        for observation in (RefObservation.OTHER, RefObservation.UNSAFE):
            with self.subTest(observation=observation):
                outcome = classify_ref_creation(
                    launched=True, process_output=ProcessOutput(1, b"", b""),
                    runner_failed=False, observation=observation,
                )
                self.assertIs(outcome, RefCreationOutcome.INDETERMINATE)

    def test_zero_exit_with_absent_ref_is_indeterminate(self) -> None:
        outcome = classify_ref_creation(
            launched=True, process_output=ProcessOutput(0, b"", b""),
            runner_failed=False, observation=RefObservation.ABSENT,
        )
        self.assertIs(outcome, RefCreationOutcome.INDETERMINATE)

    def test_inconsistent_input_shape_fails_closed(self) -> None:
        outcome = classify_ref_creation(
            launched=True, process_output=ProcessOutput(0, b"", b""),
            runner_failed=True, observation=RefObservation.EXACT,
        )
        self.assertIs(outcome, RefCreationOutcome.INDETERMINATE)
