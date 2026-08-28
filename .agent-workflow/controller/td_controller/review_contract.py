"""Structured local-review contracts and fail-closed artifact validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

ALLOWED_ROLES = frozenset({"code_review", "qa_review"})


class CodexReviewError(RuntimeError):
    """Raised when local Codex review execution or evidence fails closed."""


@dataclass(frozen=True)
class TrustedEvidence:
    """Controller-authenticated deterministic evidence supplied to QA."""

    evidence_id: str
    source: str
    description: str


@dataclass(frozen=True)
class ReviewRequest:
    """Trusted, current-SHA material supplied to a tool-less reviewer."""

    task_id: str
    role: str
    base_sha: str
    head_sha: str
    task_contract: dict[str, Any]
    diff: str
    deterministic_evidence: tuple[TrustedEvidence, ...]


@dataclass(frozen=True)
class Finding:
    """One validated code/security or QA finding."""

    finding_id: str
    severity: str
    path: str
    line: int | None
    evidence: str
    risk: str
    required_action: str
    suggested_test: str | None
    confidence: float


@dataclass(frozen=True)
class AcceptanceEvidence:
    """QA mapping from one acceptance criterion to trusted evidence."""

    criterion: str
    status: str
    evidence: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ReviewArtifact:
    """Validated structured output bound to task and Git SHAs."""

    review_type: str
    task_id: str
    base_sha: str
    head_sha: str
    verdict: str
    findings: tuple[Finding, ...]
    acceptance_evidence: tuple[AcceptanceEvidence, ...]


def _validate_trusted_evidence(evidence: tuple[TrustedEvidence, ...]) -> None:
    allowed_sources = {"github_actions", "local_controller", "local_rootless"}
    identifiers: set[str] = set()
    for item in evidence:
        evidence_id = _required_string(item.evidence_id, "evidence.id")
        _required_string(item.description, "evidence.description")
        if evidence_id in identifiers:
            raise CodexReviewError(f"duplicate trusted evidence ID: {evidence_id}")
        if item.source not in allowed_sources:
            raise CodexReviewError(f"unapproved trusted evidence source: {item.source}")
        identifiers.add(evidence_id)


def _parse_artifact(message: str, request: ReviewRequest) -> ReviewArtifact:
    role_types = {"code_review": "code_security", "qa_review": "qa"}
    if request.role not in ALLOWED_ROLES:
        raise CodexReviewError(f"unsupported local review role: {request.role}")
    if not isinstance(request.task_contract, dict):
        raise CodexReviewError("task contract must be an object")
    contract_id = request.task_contract.get("id")
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise CodexReviewError("task contract requires a non-empty ID")
    if contract_id != request.task_id:
        raise CodexReviewError("task contract ID does not match review task")
    _validate_trusted_evidence(request.deterministic_evidence)
    _validate_git_sha(request.base_sha, "request.baseSha")
    _validate_git_sha(request.head_sha, "request.headSha")
    try:
        value = json.loads(message)
    except json.JSONDecodeError as exc:
        raise CodexReviewError("review artifact is not JSON") from exc
    if not isinstance(value, dict):
        raise CodexReviewError("review artifact must be an object")
    expected_keys = {
        "reviewType",
        "taskId",
        "baseSha",
        "headSha",
        "verdict",
        "findings",
        "acceptanceEvidence",
    }
    if set(value) != expected_keys:
        raise CodexReviewError("review artifact has missing or unknown fields")
    expected_type = role_types[request.role]
    if value["reviewType"] != expected_type:
        raise CodexReviewError("review artifact has the wrong review type")
    if value["taskId"] != request.task_id:
        raise CodexReviewError("review artifact has the wrong task ID")
    if value["baseSha"] != request.base_sha or value["headSha"] != request.head_sha:
        raise CodexReviewError("review artifact references stale Git SHAs")
    if value["verdict"] not in {"pass", "block"}:
        raise CodexReviewError("review artifact has an invalid verdict")
    findings = _parse_findings(value["findings"])
    acceptance = _parse_acceptance(value["acceptanceEvidence"], request)
    if request.role == "qa_review":
        _validate_qa_acceptance(acceptance, request, verdict=value["verdict"])
        if value["verdict"] == "pass" and findings:
            raise CodexReviewError("passing QA review cannot contain findings")
    else:
        if acceptance:
            raise CodexReviewError("code/security review returned QA acceptance evidence")
        if value["verdict"] == "pass" and findings:
            raise CodexReviewError("passing review cannot contain findings")
        if value["verdict"] == "block" and not findings:
            raise CodexReviewError("blocking code review must contain findings")
    return ReviewArtifact(
        review_type=expected_type,
        task_id=request.task_id,
        base_sha=request.base_sha,
        head_sha=request.head_sha,
        verdict=value["verdict"],
        findings=findings,
        acceptance_evidence=acceptance,
    )


def _parse_findings(value: Any) -> tuple[Finding, ...]:
    if not isinstance(value, list):
        raise CodexReviewError("findings must be an array")
    findings: list[Finding] = []
    keys = {
        "id",
        "severity",
        "path",
        "line",
        "evidence",
        "risk",
        "requiredAction",
        "suggestedTest",
        "confidence",
    }
    for item in value:
        if not isinstance(item, dict) or set(item) != keys:
            raise CodexReviewError("finding has missing or unknown fields")
        if item["severity"] not in {"critical", "high", "medium", "low", "note"}:
            raise CodexReviewError("finding has invalid severity")
        confidence = item["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise CodexReviewError("finding confidence must be numeric")
        if not 0 <= confidence <= 1:
            raise CodexReviewError("finding confidence must be between zero and one")
        line = item["line"]
        if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
            raise CodexReviewError("finding line must be null or a positive integer")
        findings.append(
            Finding(
                finding_id=_required_string(item["id"], "finding.id"),
                severity=item["severity"],
                path=_required_string(item["path"], "finding.path"),
                line=line,
                evidence=_required_string(item["evidence"], "finding.evidence"),
                risk=_required_string(item["risk"], "finding.risk"),
                required_action=_required_string(
                    item["requiredAction"], "finding.requiredAction"
                ),
                suggested_test=(
                    None
                    if item["suggestedTest"] is None
                    else _required_string(item["suggestedTest"], "finding.suggestedTest")
                ),
                confidence=float(confidence),
            )
        )
    return tuple(findings)


def _parse_acceptance(
    value: Any, request: ReviewRequest
) -> tuple[AcceptanceEvidence, ...]:
    if not isinstance(value, list):
        raise CodexReviewError("acceptanceEvidence must be an array")
    trusted_ids = {item.evidence_id for item in request.deterministic_evidence}
    evidence: list[AcceptanceEvidence] = []
    for item in value:
        keys = {"criterion", "status", "evidence", "evidenceRefs"}
        if not isinstance(item, dict) or set(item) != keys:
            raise CodexReviewError("acceptance evidence has missing or unknown fields")
        if item["status"] not in {"pass", "fail", "not_tested"}:
            raise CodexReviewError("acceptance evidence has invalid status")
        references = item["evidenceRefs"]
        if not isinstance(references, list) or not references:
            raise CodexReviewError("acceptance evidence requires trusted evidence refs")
        if not all(isinstance(reference, str) for reference in references):
            raise CodexReviewError("acceptance evidence refs must be strings")
        if len(references) != len(set(references)):
            raise CodexReviewError("acceptance evidence has duplicate evidence refs")
        unknown_references = set(references) - trusted_ids
        if unknown_references:
            raise CodexReviewError(
                f"acceptance evidence has unknown refs: {sorted(unknown_references)}"
            )
        evidence.append(
            AcceptanceEvidence(
                criterion=_required_string(item["criterion"], "acceptance.criterion"),
                status=item["status"],
                evidence=_required_string(item["evidence"], "acceptance.evidence"),
                evidence_refs=tuple(references),
            )
        )
    return tuple(evidence)


def _validate_qa_acceptance(
    evidence: tuple[AcceptanceEvidence, ...],
    request: ReviewRequest,
    *,
    verdict: str,
) -> None:
    configured = request.task_contract.get("acceptanceCriteria")
    if not isinstance(configured, list) or not configured:
        raise CodexReviewError("task contract has no acceptance criteria")
    if not all(isinstance(item, str) and item.strip() for item in configured):
        raise CodexReviewError("task acceptance criteria must be non-empty strings")
    if len(configured) != len(set(configured)):
        raise CodexReviewError("task acceptance criteria must be unique")
    observed = [item.criterion for item in evidence]
    if len(observed) != len(set(observed)):
        raise CodexReviewError("QA returned duplicate acceptance criteria")
    if set(observed) != set(configured):
        raise CodexReviewError("QA must map every acceptance criterion exactly once")

    id_requirements = request.task_contract.get("acceptanceEvidenceIds")
    if not isinstance(id_requirements, dict) or set(id_requirements) != set(configured):
        raise CodexReviewError("every criterion requires controller-selected evidence IDs")
    trusted_ids = {item.evidence_id for item in request.deterministic_evidence}
    for criterion, required_ids in id_requirements.items():
        if (
            not isinstance(required_ids, list)
            or not required_ids
            or not all(isinstance(evidence_id, str) for evidence_id in required_ids)
            or len(required_ids) != len(set(required_ids))
            or not set(required_ids).issubset(trusted_ids)
        ):
            raise CodexReviewError("criterion has invalid controller-selected evidence IDs")
    for item in evidence:
        if set(item.evidence_refs) != set(id_requirements[item.criterion]):
            raise CodexReviewError("criterion evidence refs do not match controller selection")

    source_requirements = request.task_contract.get("acceptanceEvidenceRequirements", {})
    if not isinstance(source_requirements, dict):
        raise CodexReviewError("acceptance evidence requirements must be an object")
    if not set(source_requirements).issubset(configured):
        raise CodexReviewError("evidence requirements reference unknown criteria")
    evidence_sources = {
        item.evidence_id: item.source for item in request.deterministic_evidence
    }
    allowed_sources = {"github_actions", "local_controller", "local_rootless"}
    for item in evidence:
        required_sources = source_requirements.get(item.criterion, [])
        if not isinstance(required_sources, list) or not all(
            isinstance(source, str) and source in allowed_sources
            for source in required_sources
        ):
            raise CodexReviewError("criterion has invalid evidence source requirements")
        observed_sources = {evidence_sources[reference] for reference in item.evidence_refs}
        missing_sources = set(required_sources) - observed_sources
        if missing_sources:
            raise CodexReviewError(
                f"criterion lacks required evidence sources: {sorted(missing_sources)}"
            )

    has_non_passing = any(item.status != "pass" for item in evidence)
    if (verdict == "pass") == has_non_passing:
        raise CodexReviewError("QA verdict must match aggregate criterion status")


def _validate_git_sha(value: Any, field: str) -> None:
    valid = isinstance(value, str) and re.fullmatch(
        r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value
    )
    if not valid:
        raise CodexReviewError(f"{field} must be a hexadecimal Git object ID")


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodexReviewError(f"{field} must be a non-empty string")
    return value


def _artifact_to_dict(artifact: ReviewArtifact) -> dict[str, Any]:
    return {
        "reviewType": artifact.review_type,
        "taskId": artifact.task_id,
        "baseSha": artifact.base_sha,
        "headSha": artifact.head_sha,
        "verdict": artifact.verdict,
        "findings": [
            {
                "id": finding.finding_id,
                "severity": finding.severity,
                "path": finding.path,
                "line": finding.line,
                "evidence": finding.evidence,
                "risk": finding.risk,
                "requiredAction": finding.required_action,
                "suggestedTest": finding.suggested_test,
                "confidence": finding.confidence,
            }
            for finding in artifact.findings
        ],
        "acceptanceEvidence": [
            {
                "criterion": item.criterion,
                "status": item.status,
                "evidence": item.evidence,
                "evidenceRefs": list(item.evidence_refs),
            }
            for item in artifact.acceptance_evidence
        ],
    }


REVIEW_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "reviewType",
        "taskId",
        "baseSha",
        "headSha",
        "verdict",
        "findings",
        "acceptanceEvidence",
    ],
    "properties": {
        "reviewType": {"enum": ["code_security", "qa"]},
        "taskId": {"type": "string", "minLength": 1},
        "baseSha": {"type": "string", "minLength": 40, "maxLength": 64},
        "headSha": {"type": "string", "minLength": 40, "maxLength": 64},
        "verdict": {"enum": ["pass", "block"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "severity",
                    "path",
                    "line",
                    "evidence",
                    "risk",
                    "requiredAction",
                    "suggestedTest",
                    "confidence",
                ],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "severity": {
                        "enum": ["critical", "high", "medium", "low", "note"]
                    },
                    "path": {"type": "string", "minLength": 1},
                    "line": {"type": ["integer", "null"], "minimum": 1},
                    "evidence": {"type": "string", "minLength": 1},
                    "risk": {"type": "string", "minLength": 1},
                    "requiredAction": {"type": "string", "minLength": 1},
                    "suggestedTest": {"type": ["string", "null"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
        "acceptanceEvidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["criterion", "status", "evidence", "evidenceRefs"],
                "properties": {
                    "criterion": {"type": "string", "minLength": 1},
                    "status": {"enum": ["pass", "fail", "not_tested"]},
                    "evidence": {"type": "string", "minLength": 1},
                    "evidenceRefs": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
    },
}
