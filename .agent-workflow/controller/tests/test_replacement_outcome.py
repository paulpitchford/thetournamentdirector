from __future__ import annotations

import traceback
import unittest

from td_controller.attested_payload_runner import AttestedPayloadRunnerError
from td_controller.replacement_outcome import (
    APPLIED_SENTINEL,
    INDETERMINATE_SENTINEL,
    ReplacementIndeterminateError,
    ReplacementOutcome,
    ReplacementOutcomeError,
    classify_replacement_output,
    require_applied,
    require_definitive_rejection,
)
from td_controller.review_runtime import ProcessOutput


class ReplacementOutcomeTests(unittest.TestCase):
    def test_only_exact_success_shape_is_applied(self) -> None:
        applied = ProcessOutput(0, APPLIED_SENTINEL, b"")
        ambiguous = (
            ProcessOutput(0, b"", b""),
            ProcessOutput(0, APPLIED_SENTINEL + b"extra", b""),
            ProcessOutput(0, APPLIED_SENTINEL, b"warning"),
            ProcessOutput(52, INDETERMINATE_SENTINEL, b""),
        )

        self.assertIs(
            classify_replacement_output(applied), ReplacementOutcome.APPLIED
        )
        for output in ambiguous:
            with self.subTest(output=output):
                self.assertIs(
                    classify_replacement_output(output),
                    ReplacementOutcome.INDETERMINATE,
                )

    def test_only_silent_precondition_exits_are_rejected(self) -> None:
        for returncode in (1, 51):
            with self.subTest(returncode=returncode):
                self.assertIs(
                    classify_replacement_output(
                        ProcessOutput(returncode, b"", b"")
                    ),
                    ReplacementOutcome.REJECTED,
                )
        for output in (
            ProcessOutput(1, b"detail", b""),
            ProcessOutput(51, b"", b"detail"),
            ProcessOutput(2, b"", b""),
            ProcessOutput(137, b"", b""),
        ):
            with self.subTest(output=output):
                self.assertIs(
                    classify_replacement_output(output),
                    ReplacementOutcome.INDETERMINATE,
                )

    def test_applied_requirement_distinguishes_all_outcomes(self) -> None:
        require_applied(lambda: ProcessOutput(0, APPLIED_SENTINEL, b""))
        with self.assertRaisesRegex(ReplacementOutcomeError, "rejected"):
            require_applied(lambda: ProcessOutput(1, b"", b""))
        with self.assertRaises(ReplacementIndeterminateError):
            require_applied(lambda: ProcessOutput(52, b"", b""))

    def test_negative_proof_refuses_applied_and_indeterminate_outcomes(self) -> None:
        require_definitive_rejection(
            lambda: ProcessOutput(51, b"", b"")
        )
        with self.assertRaisesRegex(ReplacementOutcomeError, "accepted"):
            require_definitive_rejection(
                lambda: ProcessOutput(0, APPLIED_SENTINEL, b"")
            )
        with self.assertRaisesRegex(
            ReplacementIndeterminateError, "indeterminate"
        ):
            require_definitive_rejection(
                lambda: ProcessOutput(52, INDETERMINATE_SENTINEL, b"")
            )

    def test_runner_failure_is_indeterminate_and_diagnostic_safe(self) -> None:
        secret = "runner-diagnostic-must-not-escape"

        def fail():
            raise AttestedPayloadRunnerError(secret)

        for requirement in (require_applied, require_definitive_rejection):
            with self.subTest(requirement=requirement):
                with self.assertRaises(ReplacementIndeterminateError) as raised:
                    requirement(fail)
                self.assertNotIn(secret, str(raised.exception))
                rendered = "".join(
                    traceback.format_exception(raised.exception)
                )
                self.assertNotIn(secret, rendered)
                self.assertIsNone(raised.exception.__cause__)
                self.assertIsNone(raised.exception.__context__)

    def test_invalid_operation_and_output_fail_closed(self) -> None:
        with self.assertRaisesRegex(ReplacementOutcomeError, "operation"):
            require_applied(object())
        self.assertIs(
            classify_replacement_output(object()),
            ReplacementOutcome.INDETERMINATE,
        )
