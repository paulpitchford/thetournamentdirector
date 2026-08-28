# ADR 0001: Use Codex through Sandcastle, with Copilot PR review

- **Status:** Accepted; Copilot-review decision superseded by ADR 0007
- **Date:** 2026-08-28

## Context

Codex is the only available coding agent for Pi, Sandcastle, and local agent
runs. GitHub Copilot is available only for pull-request code review; Copilot
cloud/coding agent is not available for implementation or remediation. The user
does not want to create or manage routine PRs manually.

## Decision

Use Codex as the implementation, planning, remediation, code-review, security,
and QA agent through separate Pi/Sandcastle sessions. Use
`@ai-hero/sandcastle` as the replaceable execution adapter for isolated
worktrees, bounded runs, logs, commits, and structured outputs.

Use GitHub Copilot code review as an additional independent review signal on
every PR. Copilot is not an implementer and does not repair findings.

The deterministic controller owns tasks, worktrees, branches, pushes, PR
creation, capacity, policy checks, review status, remediation routing, and merge
eligibility. GitHub Actions and branch rules remain authoritative.

Use explicit named branches for unattended Sandcastle work. Do not use `head`,
`merge-to-head`, or `noSandbox()` execution.

## Consequences

- All code-changing capacity comes from Codex, so implementation and required
  Codex reviews need one shared quota and explicit review reserve.
- Separate Codex sessions reduce context/self-review coupling but are not true
  model diversity; Copilot PR review adds an independent provider signal.
- The controller—not the user—pushes task branches and creates/updates PRs.
- Copilot review always returns comments rather than approval/request-changes,
  so it cannot directly satisfy or block GitHub required approvals. The
  controller must convert completion and unresolved findings into a required
  policy status.
- Codex completion and Copilot comments never imply acceptance by themselves.
- Sandcastle remains replaceable if the pilot exposes security, reliability, or
  maintenance problems.
- A hardened sandbox provider may be needed for local unattended agents.

## Alternatives considered

- Copilot coding/cloud agent for implementation: unavailable in the user's
  setup.
- Copilot self-review and implementation: unavailable and not independent.
- Gas Town as the whole control plane: heavier than needed for the first pilot.
- Build all sandbox/provider plumbing ourselves: unnecessary initial scope.
