"""Strict read-only planner output and dependency-graph validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .task_contract import TaskContract, TaskContractError, _path_claims_overlap, parse_task

PLAN_KEYS = frozenset(
    {"planId", "baseSha", "backlogSha256", "tasks", "parallelGroups", "assumptions"}
)
PLAN_ID = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PlanContractError(ValueError):
    """Raised when planner output is ambiguous or unsafe to propose."""


class _DuplicateKey(ValueError):
    pass


@dataclass(frozen=True)
class PlanContract:
    plan_id: str
    base_sha: str
    backlog_sha256: str
    tasks: tuple[TaskContract, ...]
    parallel_groups: tuple[tuple[str, ...], ...]
    assumptions: tuple[str, ...]
    tasks_by_id: Mapping[str, TaskContract]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def parse_plan_json(
    payload: str | bytes,
    *,
    expected_base_sha: str,
    expected_backlog_sha256: str,
    known_task_ids: frozenset[str] = frozenset(),
) -> PlanContract:
    if not isinstance(payload, (str, bytes)) or len(payload) > 512_000:
        raise PlanContractError("plan payload exceeds the size limit")
    if isinstance(payload, str):
        try:
            encoded_size = len(payload.encode("utf-8"))
        except UnicodeError as exc:
            raise PlanContractError("plan is not unambiguous JSON") from exc
        if encoded_size > 512_000:
            raise PlanContractError("plan payload exceeds the size limit")
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PlanContractError("plan is not unambiguous JSON") from exc
    return parse_plan(
        value,
        expected_base_sha=expected_base_sha,
        expected_backlog_sha256=expected_backlog_sha256,
        known_task_ids=known_task_ids,
    )


def parse_plan(
    value: object,
    *,
    expected_base_sha: str,
    expected_backlog_sha256: str,
    known_task_ids: frozenset[str] = frozenset(),
) -> PlanContract:
    if not isinstance(value, dict) or set(value) != PLAN_KEYS:
        raise PlanContractError("plan fields do not match the contract")
    plan_id = _required(value["planId"], "planId")
    if PLAN_ID.fullmatch(plan_id) is None:
        raise PlanContractError("plan ID is invalid")
    base_sha = _required(value["baseSha"], "baseSha")
    backlog_sha = _required(value["backlogSha256"], "backlogSha256")
    if GIT_SHA.fullmatch(base_sha) is None or SHA256.fullmatch(backlog_sha) is None:
        raise PlanContractError("plan source identity is invalid")
    if base_sha != expected_base_sha or backlog_sha != expected_backlog_sha256:
        raise PlanContractError("plan source identity does not match trusted input")
    raw_tasks = value["tasks"]
    if not isinstance(raw_tasks, list) or not raw_tasks or len(raw_tasks) > 100:
        raise PlanContractError("plan tasks must be a bounded non-empty list")
    try:
        tasks = tuple(parse_task(item) for item in raw_tasks)
    except TaskContractError as exc:
        raise PlanContractError("plan contains an invalid task") from exc
    if any(task.status != "PROPOSED" or not task.human_approval_required for task in tasks):
        raise PlanContractError("planner tasks must remain proposed for human approval")
    tasks_by_id = {task.task_id: task for task in tasks}
    if len(tasks_by_id) != len(tasks):
        raise PlanContractError("plan contains duplicate task IDs")
    available = set(tasks_by_id) | set(known_task_ids)
    for task in tasks:
        if any(dependency not in available for dependency in task.depends_on):
            raise PlanContractError("plan contains an unknown dependency")
    _reject_cycles(tasks_by_id)
    groups = _parallel_groups(value["parallelGroups"], tasks_by_id)
    assumptions = _string_list(value["assumptions"], "assumptions", allow_empty=True)
    return PlanContract(
        plan_id, base_sha, backlog_sha, tasks, groups, assumptions,
        MappingProxyType(tasks_by_id),
    )


def _reject_cycles(tasks: dict[str, TaskContract]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise PlanContractError("plan dependency graph contains a cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in tasks[task_id].depends_on:
            if dependency in tasks:
                visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)


def _parallel_groups(
    value: object, tasks: dict[str, TaskContract]
) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list) or len(value) > 50:
        raise PlanContractError("parallelGroups must be a bounded list")
    groups: list[tuple[str, ...]] = []
    assigned: set[str] = set()
    dependencies = {
        task_id: _reachable_dependencies(task_id, tasks) for task_id in tasks
    }
    for raw_group in value:
        group = _string_list(raw_group, "parallelGroups")
        if len(group) < 2 or not set(group).issubset(tasks) or assigned & set(group):
            raise PlanContractError("parallel group membership is invalid")
        for index, left_id in enumerate(group):
            for right_id in group[index + 1:]:
                left = tasks[left_id]
                right = tasks[right_id]
                if right_id in dependencies[left_id] or left_id in dependencies[right_id]:
                    raise PlanContractError("parallel tasks have a dependency relationship")
                if any(
                    _path_claims_overlap(left_path, right_path)
                    for left_path in left.allowed_paths for right_path in right.allowed_paths
                ):
                    raise PlanContractError("parallel task path claims overlap")
        assigned.update(group)
        groups.append(group)
    return tuple(groups)


def _reachable_dependencies(
    task_id: str, tasks: dict[str, TaskContract]
) -> frozenset[str]:
    reachable: set[str] = set()
    pending = [dependency for dependency in tasks[task_id].depends_on if dependency in tasks]
    while pending:
        dependency = pending.pop()
        if dependency in reachable:
            continue
        reachable.add(dependency)
        pending.extend(
            child for child in tasks[dependency].depends_on
            if child in tasks and child not in reachable
        )
    return frozenset(reachable)


def _required(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 2_000:
        raise PlanContractError(f"{field} must be canonical bounded text")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise PlanContractError(f"{field} is not valid Unicode") from exc
    return value


def _string_list(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty) or len(value) > 100:
        raise PlanContractError(f"{field} must be a bounded list")
    items = tuple(_required(item, field) for item in value)
    if len(items) != len(set(items)):
        raise PlanContractError(f"{field} contains duplicates")
    return items
