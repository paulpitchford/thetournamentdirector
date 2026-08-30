"""Closed contract for controller-selected text replacement proposals."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from .task_contract import TaskContract, tracked_path_is_allowed

SHA = re.compile(r"[0-9a-f]{40}")
NONCE = re.compile(r"[0-9a-f]{32}")
MAX_FILE_BYTES = 100_000
MAX_TOTAL_BYTES = 400_000
MAX_FILES = 8

MUTATION_PROPOSAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["taskId", "baseSha", "nonce", "summary", "replacements"],
    "properties": {
        "taskId": {"type": "string", "minLength": 3, "maxLength": 128},
        "baseSha": {"type": "string", "minLength": 40, "maxLength": 64},
        "nonce": {"type": "string", "minLength": 32, "maxLength": 32},
        "summary": {"type": "string", "minLength": 1, "maxLength": 1000},
        "replacements": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_FILES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "expectedSha256", "content"],
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "expectedSha256": {
                        "type": "string", "minLength": 64, "maxLength": 64,
                    },
                    "content": {
                        "type": "string", "maxLength": MAX_FILE_BYTES,
                    },
                },
            },
        },
    },
}


class TextMutationContractError(ValueError):
    """Raised when selected input or a model proposal is unsafe."""


@dataclass(frozen=True)
class SelectedTextFile:
    """One controller-selected existing file supplied to the model broker."""

    path: str
    sha256: str
    content: str


@dataclass(frozen=True)
class TextMutationRequest:
    """Immutable controller-bound input for one proposal session."""

    task: TaskContract
    base_sha: str
    nonce: str
    files: Mapping[str, SelectedTextFile]


@dataclass(frozen=True)
class TextReplacement:
    """Validated whole-file replacement for one selected path."""

    path: str
    expected_sha256: str
    content: str


@dataclass(frozen=True)
class TextMutationProposal:
    """Validated declarative proposal with no executable operations."""

    task_id: str
    base_sha: str
    nonce: str
    summary: str
    replacements: tuple[TextReplacement, ...]
    changed_lines: int


def build_text_mutation_request(
    task: TaskContract,
    *,
    base_sha: str,
    nonce: str,
    files: Sequence[SelectedTextFile],
) -> TextMutationRequest:
    """Validate controller-selected existing UTF-8 files."""
    if (
        not isinstance(task, TaskContract)
        or task.status not in {"APPROVED", "QUEUED"}
    ):
        raise TextMutationContractError("mutation task is invalid")
    if not isinstance(base_sha, str) or not SHA.fullmatch(base_sha):
        raise TextMutationContractError("mutation base SHA is invalid")
    if not isinstance(nonce, str) or not NONCE.fullmatch(nonce):
        raise TextMutationContractError("mutation nonce is invalid")
    if (
        isinstance(files, (str, bytes))
        or not isinstance(files, Sequence)
        or not 1 <= len(files) <= MAX_FILES
    ):
        raise TextMutationContractError("selected file collection is invalid")
    selected: dict[str, SelectedTextFile] = {}
    total = 0
    for item in files:
        if not isinstance(item, SelectedTextFile):
            raise TextMutationContractError("selected file is invalid")
        content_bytes = _text_bytes(item.content, "selected content")
        if len(content_bytes) > MAX_FILE_BYTES:
            raise TextMutationContractError("selected file exceeds the size limit")
        if (
            not tracked_path_is_allowed(task, item.path)
            or not re.fullmatch(r"[0-9a-f]{64}", item.sha256)
            or hashlib.sha256(content_bytes).hexdigest() != item.sha256
        ):
            raise TextMutationContractError("selected file identity is invalid")
        if item.path in selected:
            raise TextMutationContractError("selected paths contain duplicates")
        selected[item.path] = item
        total += len(content_bytes)
    if total > MAX_TOTAL_BYTES:
        raise TextMutationContractError("selected files exceed the total size limit")
    return TextMutationRequest(
        task, base_sha, nonce, MappingProxyType(selected)
    )


def parse_text_mutation_proposal(
    value: object, request: TextMutationRequest
) -> TextMutationProposal:
    """Bind a decoded broker response to exact selected file identities."""
    if not isinstance(request, TextMutationRequest) or not isinstance(value, dict):
        raise TextMutationContractError("mutation proposal is invalid")
    required = {"taskId", "baseSha", "nonce", "summary", "replacements"}
    if set(value) != required:
        raise TextMutationContractError("mutation proposal fields are invalid")
    if (
        value["taskId"] != request.task.task_id
        or value["baseSha"] != request.base_sha
        or value["nonce"] != request.nonce
    ):
        raise TextMutationContractError("mutation proposal identity is stale")
    summary = _bounded_text(
        value["summary"], "summary", 1000, allow_empty=False
    )
    if summary != summary.strip():
        raise TextMutationContractError("summary is invalid")
    raw_replacements = value["replacements"]
    if not isinstance(raw_replacements, list) or not 1 <= len(raw_replacements) <= MAX_FILES:
        raise TextMutationContractError("replacement collection is invalid")
    replacements: list[TextReplacement] = []
    paths: set[str] = set()
    changed_lines = 0
    total = 0
    for raw in raw_replacements:
        if not isinstance(raw, dict) or set(raw) != {"path", "expectedSha256", "content"}:
            raise TextMutationContractError("replacement fields are invalid")
        path = raw["path"]
        expected = raw["expectedSha256"]
        if not isinstance(path, str) or path not in request.files or path in paths:
            raise TextMutationContractError("replacement path is invalid")
        selected = request.files[path]
        if expected != selected.sha256:
            raise TextMutationContractError("replacement identity is stale")
        content = _bounded_text(raw["content"], "replacement content", MAX_FILE_BYTES)
        content_bytes = _text_bytes(content, "replacement content")
        if len(content_bytes) > MAX_FILE_BYTES:
            raise TextMutationContractError("replacement exceeds the size limit")
        if content == selected.content:
            raise TextMutationContractError("replacement does not change the file")
        total += len(content_bytes)
        changed_lines += _changed_lines(selected.content, content)
        paths.add(path)
        replacements.append(TextReplacement(path, expected, content))
    if total > MAX_TOTAL_BYTES:
        raise TextMutationContractError("replacements exceed the total size limit")
    if changed_lines > request.task.max_changed_lines:
        raise TextMutationContractError("proposal exceeds the changed-line limit")
    return TextMutationProposal(
        request.task.task_id, request.base_sha, request.nonce, summary,
        tuple(replacements), changed_lines,
    )


def _bounded_text(value: object, field: str, limit: int, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > limit or (not allow_empty and not value.strip()):
        raise TextMutationContractError(f"{field} is invalid")
    _text_bytes(value, field)
    return value


def _text_bytes(value: object, field: str) -> bytes:
    if not isinstance(value, str) or "\x00" in value:
        raise TextMutationContractError(f"{field} is invalid")
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise TextMutationContractError(f"{field} is invalid") from exc


def _changed_lines(before: str, after: str) -> int:
    old_lines = before.splitlines(keepends=True)
    new_lines = after.splitlines(keepends=True)
    prefix = 0
    while (
        prefix < len(old_lines)
        and prefix < len(new_lines)
        and old_lines[prefix] == new_lines[prefix]
    ):
        prefix += 1
    suffix = 0
    while (
        suffix < len(old_lines) - prefix
        and suffix < len(new_lines) - prefix
        and old_lines[-1 - suffix] == new_lines[-1 - suffix]
    ):
        suffix += 1
    return (
        len(old_lines) - prefix - suffix
        + len(new_lines) - prefix - suffix
    )
