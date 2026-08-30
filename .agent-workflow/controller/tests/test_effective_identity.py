from __future__ import annotations

import os
import unittest

from td_controller.effective_identity import (
    EffectiveIdentity,
    EffectiveIdentityError,
    validate_effective_identity,
)


class EffectiveIdentityTests(unittest.TestCase):
    def test_current_process_identity_is_unambiguous(self) -> None:
        identity = validate_effective_identity()
        self.assertEqual(identity, EffectiveIdentity(os.geteuid(), os.getegid()))

    def test_real_effective_uid_or_gid_mismatch_is_rejected(self) -> None:
        cases = (
            {"getuid": lambda: 1000, "geteuid": lambda: 0},
            {"getgid": lambda: 1000, "getegid": lambda: 0},
        )
        defaults = {
            "getuid": lambda: 1000, "geteuid": lambda: 1000,
            "getgid": lambda: 1000, "getegid": lambda: 1000,
        }
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(
                    EffectiveIdentityError, "transition"
                ):
                    validate_effective_identity(**(defaults | changes))

    def test_invalid_values_and_getters_fail_closed(self) -> None:
        for value in (-1, True, "1000"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(EffectiveIdentityError, "invalid"):
                    validate_effective_identity(
                        getuid=lambda value=value: value,
                        geteuid=lambda value=value: value,
                        getgid=lambda: 1000,
                        getegid=lambda: 1000,
                    )
        with self.assertRaisesRegex(EffectiveIdentityError, "getter"):
            validate_effective_identity(getuid=object())

    def test_getter_failure_is_normalized_without_diagnostic_chain(self) -> None:
        secret = "identity-diagnostic"

        def fail():
            raise OSError(secret)

        with self.assertRaises(EffectiveIdentityError) as raised:
            validate_effective_identity(getuid=fail)
        self.assertNotIn(secret, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
