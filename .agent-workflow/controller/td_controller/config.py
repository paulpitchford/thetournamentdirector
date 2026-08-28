"""Strict parser for the reviewed orchestration pilot configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when reviewed controller configuration is malformed."""


@dataclass(frozen=True)
class ProviderConfig:
    """Approved model-provider route."""

    name: str
    model: str
    reasoning: str
    fallback: None


@dataclass(frozen=True)
class LimitConfig:
    """Hard admission and retention limits."""

    max_concurrent_runs: int
    max_run_minutes: int
    max_runs_per_day: int
    review_reserve_percent: int
    max_remediation_rounds: int
    retention_days: int


@dataclass(frozen=True)
class RunnerConfig:
    """Required local sandbox boundary."""

    runtime: str
    rootless_required: bool
    socket: Path
    network_default: str


@dataclass(frozen=True)
class ControllerConfig:
    """Trusted host-controller paths and integration mode."""

    pause_file: Path
    database: Path
    credential: Path
    pull_request_creation: str


@dataclass(frozen=True)
class PilotConfig:
    """Complete approved pilot configuration."""

    version: str
    provider: ProviderConfig
    limits: LimitConfig
    runner: RunnerConfig
    controller: ControllerConfig


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{field} must be an object with string keys")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], field: str) -> None:
    missing = expected - value.keys()
    unknown = value.keys() - expected
    if missing or unknown:
        raise ConfigError(
            f"{field} keys invalid; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    return value


def _integer(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{field} must be between {minimum} and {maximum}")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{field} must be a boolean")
    return value


def _approved_path(value: Any, field: str, approved: Path) -> Path:
    path = Path(_string(value, field))
    if path != approved:
        raise ConfigError(f"{field} must be {approved}")
    return path


def parse_config(value: Any) -> PilotConfig:
    """Parse and validate an untrusted decoded JSON value."""
    root = _object(value, "config")
    _exact_keys(root, {"version", "provider", "limits", "runner", "controller"}, "config")

    provider = _object(root["provider"], "provider")
    _exact_keys(provider, {"name", "model", "reasoning", "fallback"}, "provider")
    if provider["name"] != "codex":
        raise ConfigError("provider.name must be codex")
    if provider["model"] != "configured-default":
        raise ConfigError("provider.model must be configured-default")
    if provider["reasoning"] != "high":
        raise ConfigError("provider.reasoning must be high")
    if provider["fallback"] is not None:
        raise ConfigError("provider.fallback must be null during the pilot")

    limits = _object(root["limits"], "limits")
    _exact_keys(
        limits,
        {
            "maxConcurrentRuns",
            "maxRunMinutes",
            "maxRunsPerDay",
            "reviewReservePercent",
            "maxRemediationRounds",
            "retentionDays",
        },
        "limits",
    )

    runner = _object(root["runner"], "runner")
    _exact_keys(
        runner,
        {"runtime", "rootlessRequired", "socket", "networkDefault"},
        "runner",
    )
    if runner["runtime"] != "podman":
        raise ConfigError("runner.runtime must be podman")
    if _boolean(runner["rootlessRequired"], "runner.rootlessRequired") is not True:
        raise ConfigError("runner.rootlessRequired must be true")
    socket = Path(_string(runner["socket"], "runner.socket"))
    if socket != Path("/run/user/1000/podman/podman.sock"):
        raise ConfigError("runner.socket must match the approved user socket")
    if runner["networkDefault"] != "none":
        raise ConfigError("runner.networkDefault must be none")

    controller = _object(root["controller"], "controller")
    _exact_keys(
        controller,
        {"pauseFile", "database", "credential", "pullRequestCreation"},
        "controller",
    )
    if controller["pullRequestCreation"] != "github-actions-create-event":
        raise ConfigError("controller.pullRequestCreation is not approved")

    version = _string(root["version"], "version")
    if version != "1":
        raise ConfigError(f"unsupported config version: {version}")

    return PilotConfig(
        version=version,
        provider=ProviderConfig(
            name="codex",
            model="configured-default",
            reasoning="high",
            fallback=None,
        ),
        limits=LimitConfig(
            max_concurrent_runs=_integer(
                limits["maxConcurrentRuns"],
                "limits.maxConcurrentRuns",
                minimum=1,
                maximum=1,
            ),
            max_run_minutes=_integer(
                limits["maxRunMinutes"],
                "limits.maxRunMinutes",
                minimum=60,
                maximum=60,
            ),
            max_runs_per_day=_integer(
                limits["maxRunsPerDay"],
                "limits.maxRunsPerDay",
                minimum=8,
                maximum=8,
            ),
            review_reserve_percent=_integer(
                limits["reviewReservePercent"],
                "limits.reviewReservePercent",
                minimum=30,
                maximum=30,
            ),
            max_remediation_rounds=_integer(
                limits["maxRemediationRounds"],
                "limits.maxRemediationRounds",
                minimum=2,
                maximum=2,
            ),
            retention_days=_integer(
                limits["retentionDays"],
                "limits.retentionDays",
                minimum=14,
                maximum=14,
            ),
        ),
        runner=RunnerConfig(
            runtime="podman",
            rootless_required=True,
            socket=socket,
            network_default="none",
        ),
        controller=ControllerConfig(
            pause_file=_approved_path(
                controller["pauseFile"],
                "controller.pauseFile",
                Path(".agent-workflow/state/PAUSED"),
            ),
            database=_approved_path(
                controller["database"],
                "controller.database",
                Path(".agent-workflow/state/controller.sqlite3"),
            ),
            credential=_approved_path(
                controller["credential"],
                "controller.credential",
                Path(".agent-workflow/state/credentials/controller_ed25519"),
            ),
            pull_request_creation="github-actions-create-event",
        ),
    )


def load_config(path: Path) -> PilotConfig:
    """Load strict pilot configuration from ``path``."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot load configuration {path}: {exc}") from exc
    return parse_config(value)
