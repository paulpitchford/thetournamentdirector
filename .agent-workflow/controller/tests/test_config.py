"""Tests for strict pilot-configuration validation."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from td_controller.config import ConfigError, parse_config

CONFIG_PATH = Path(__file__).resolve().parents[2] / "policy/pilot.json"


def valid_config() -> dict[str, object]:
    """Return an independent decoded copy of the reviewed pilot config."""
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


class ConfigTests(unittest.TestCase):
    """Prove approved values pass and policy weakening fails closed."""

    def test_reviewed_config_is_accepted(self) -> None:
        config = parse_config(valid_config())

        self.assertEqual(config.provider.name, "codex")
        self.assertEqual(config.limits.max_runs_per_day, 8)
        self.assertEqual(config.limits.review_reserve_percent, 30)
        self.assertTrue(config.runner.rootless_required)
        self.assertFalse(config.reviews.model_reviews_on_github)
        self.assertEqual(config.reviews.required_roles, ("code_review", "qa_review"))

    def test_unknown_key_is_rejected(self) -> None:
        value = valid_config()
        value["unexpected"] = True

        with self.assertRaisesRegex(ConfigError, "unknown=.*unexpected"):
            parse_config(value)

    def test_weaker_review_reserve_is_rejected(self) -> None:
        value = copy.deepcopy(valid_config())
        limits = value["limits"]
        self.assertIsInstance(limits, dict)
        limits["reviewReservePercent"] = 0

        with self.assertRaisesRegex(ConfigError, "between 30 and 30"):
            parse_config(value)

    def test_changed_approved_run_limit_is_rejected(self) -> None:
        value = copy.deepcopy(valid_config())
        limits = value["limits"]
        self.assertIsInstance(limits, dict)
        limits["maxRunMinutes"] = 30

        with self.assertRaisesRegex(ConfigError, "between 60 and 60"):
            parse_config(value)

    def test_github_model_review_is_rejected(self) -> None:
        value = copy.deepcopy(valid_config())
        reviews = value["reviews"]
        self.assertIsInstance(reviews, dict)
        reviews["modelReviewsOnGitHub"] = True

        with self.assertRaisesRegex(ConfigError, "must be false"):
            parse_config(value)

    def test_fallback_provider_is_rejected(self) -> None:
        value = copy.deepcopy(valid_config())
        provider = value["provider"]
        self.assertIsInstance(provider, dict)
        provider["fallback"] = "another-provider"

        with self.assertRaisesRegex(ConfigError, "fallback must be null"):
            parse_config(value)

    def test_escaping_controller_path_is_rejected(self) -> None:
        value = copy.deepcopy(valid_config())
        controller = value["controller"]
        self.assertIsInstance(controller, dict)
        controller["database"] = "../outside.sqlite3"

        with self.assertRaisesRegex(ConfigError, "must be .agent-workflow/state"):
            parse_config(value)


if __name__ == "__main__":
    unittest.main()
