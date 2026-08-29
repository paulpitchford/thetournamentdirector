"""Strict dispatchable-task contract parsing and deterministic validation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

TASK_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")
RISK_CLASSES = frozenset({"R0", "R1", "R2", "R3"})
TASK_STATES = frozenset(
    {
        "PROPOSED", "SPEC_REVIEW", "APPROVED", "QUEUED", "LEASED",
        "IMPLEMENTING", "VERIFYING", "PR_DRAFT", "REVIEWING", "REMEDIATING",
        "CI_PENDING", "READY_FOR_POLICY_MERGE", "AUTO_MERGE_PENDING", "MERGED",
        "DONE", "BLOCKED_REQUIREMENTS", "BLOCKED_DEPENDENCY", "FAILED_RETRYABLE",
        "QUARANTINED", "CANCELLED", "SUPERSEDED",
    }
)
EVIDENCE_SOURCES = frozenset({"github_actions", "local_controller", "local_rootless"})
OPTIONAL_KEYS = frozenset({"acceptanceEvidenceIds", "acceptanceEvidenceRequirements"})
REQUIRED_KEYS = frozenset(
    {
        "id", "status", "parentEpic", "objective", "nonGoals", "dependsOn",
        "acceptanceCriteria", "requiredTests", "allowedPaths", "protectedPaths",
        "riskClass", "maxChangedLines", "maxAttempts", "humanApprovalRequired",
    }
)
VAGUE_CRITERION = re.compile(
    r"\b(works? well|properly|as expected|user[- ]friendly|robust|high quality)\b",
    re.IGNORECASE,
)
PROHIBITED_ROOTS = frozenset({"downloads", "extracted", "analysis"})


class TaskContractError(ValueError):
    """Raised when a task cannot be trusted for dispatch."""


class _DuplicateKey(ValueError):
    pass


@dataclass(frozen=True)
class TaskContract:
    task_id: str
    status: str
    parent_epic: str
    objective: str
    non_goals: tuple[str, ...]
    depends_on: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    required_tests: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    risk_class: str
    max_changed_lines: int
    max_attempts: int
    human_approval_required: bool
    acceptance_evidence_ids: Mapping[str, tuple[str, ...]]
    acceptance_evidence_requirements: Mapping[str, tuple[str, ...]]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey(key)
        value[key] = item
    return value


def parse_task_json(payload: str | bytes) -> TaskContract:
    """Parse unambiguous JSON and return a validated immutable task."""
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError, _DuplicateKey) as exc:
        raise TaskContractError("task is not unambiguous JSON") from exc
    return parse_task(value)


def load_task(path: Path) -> TaskContract:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise TaskContractError("task file is unavailable") from exc
    if len(payload) > 256_000:
        raise TaskContractError("task file exceeds the size limit")
    return parse_task_json(payload)


def parse_task(value: object) -> TaskContract:
    """Validate a decoded task object without coercing untrusted values."""
    if not isinstance(value, dict):
        raise TaskContractError("task must be an object")
    keys = set(value)
    if keys - REQUIRED_KEYS - OPTIONAL_KEYS or REQUIRED_KEYS - keys:
        raise TaskContractError("task fields do not match the contract")

    task_id = _identifier(value["id"], "id")
    parent_epic = _identifier(value["parentEpic"], "parentEpic")
    status = value["status"]
    if not isinstance(status, str) or status not in TASK_STATES:
        raise TaskContractError("task status is invalid")
    risk_class = value["riskClass"]
    if not isinstance(risk_class, str) or risk_class not in RISK_CLASSES:
        raise TaskContractError("task risk class is invalid")
    objective = _text(value["objective"], "objective")
    non_goals = _string_list(value["nonGoals"], "nonGoals", allow_empty=True)
    depends_on = _identifier_list(value["dependsOn"], "dependsOn")
    if task_id in depends_on:
        raise TaskContractError("task cannot depend on itself")
    criteria = _string_list(value["acceptanceCriteria"], "acceptanceCriteria")
    if any(VAGUE_CRITERION.search(item) for item in criteria):
        raise TaskContractError("acceptance criterion is vague")
    required_tests = _string_list(value["requiredTests"], "requiredTests")
    allowed_paths = _path_list(value["allowedPaths"], "allowedPaths")
    protected_paths = _path_list(value["protectedPaths"], "protectedPaths")
    if set(allowed_paths) & set(protected_paths):
        raise TaskContractError("allowed and protected paths overlap exactly")
    max_lines = _bounded_int(value["maxChangedLines"], "maxChangedLines", 1, 5_000)
    max_attempts = _bounded_int(value["maxAttempts"], "maxAttempts", 1, 5)
    approval = value["humanApprovalRequired"]
    if not isinstance(approval, bool):
        raise TaskContractError("humanApprovalRequired must be boolean")
    evidence_ids = _criterion_mapping(
        value.get("acceptanceEvidenceIds", {}), criteria, "acceptanceEvidenceIds"
    )
    requirements = _criterion_mapping(
        value.get("acceptanceEvidenceRequirements", {}),
        criteria,
        "acceptanceEvidenceRequirements",
        allowed_values=EVIDENCE_SOURCES,
    )
    return TaskContract(
        task_id, status, parent_epic, objective, non_goals, depends_on, criteria,
        required_tests, allowed_paths, protected_paths, risk_class, max_lines,
        max_attempts, approval, MappingProxyType(evidence_ids),
        MappingProxyType(requirements),
    )


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2_000:
        raise TaskContractError(f"{field} must be bounded non-empty text")
    if "\x00" in value:
        raise TaskContractError(f"{field} contains a null byte")
    return value


def _identifier(value: object, field: str) -> str:
    text = _text(value, field)
    if not TASK_ID_PATTERN.fullmatch(text):
        raise TaskContractError(f"{field} is not a valid identifier")
    return text


def _string_list(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty) or len(value) > 200:
        raise TaskContractError(f"{field} must be a bounded list")
    items = tuple(_text(item, field) for item in value)
    if len(items) != len(set(items)):
        raise TaskContractError(f"{field} contains duplicates")
    return items


def _identifier_list(value: object, field: str) -> tuple[str, ...]:
    items = _string_list(value, field, allow_empty=True)
    if any(not TASK_ID_PATTERN.fullmatch(item) for item in items):
        raise TaskContractError(f"{field} contains an invalid identifier")
    return items


def _path_list(value: object, field: str) -> tuple[str, ...]:
    items = _string_list(value, field)
    for item in items:
        path = PurePosixPath(item)
        root = item.split("/", 1)[0]
        if item.startswith("/") or ".." in path.parts or root in PROHIBITED_ROOTS:
            raise TaskContractError(f"{field} contains a prohibited path")
        if item.startswith("./") or "//" in item or "\\" in item:
            raise TaskContractError(f"{field} contains a non-canonical path")
    return items


def _bounded_int(value: object, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise TaskContractError(f"{field} is outside its allowed range")
    return value


def _criterion_mapping(
    value: object,
    criteria: tuple[str, ...],
    field: str,
    *,
    allowed_values: frozenset[str] | None = None,
) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict) or not set(value).issubset(criteria):
        raise TaskContractError(f"{field} references an unknown criterion")
    result: dict[str, tuple[str, ...]] = {}
    for criterion, raw_items in value.items():
        items = _string_list(raw_items, field)
        if allowed_values is not None and not set(items).issubset(allowed_values):
            raise TaskContractError(f"{field} contains an unapproved source")
        result[criterion] = items
    return result
