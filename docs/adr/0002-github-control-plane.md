# ADR 0002: Automate the GitHub issue and pull-request lifecycle

- **Status:** Accepted
- **Date:** 2026-08-28

## Context

Agent work needs a visible approved queue, immutable integration record,
independent CI, review evidence, and protected merge boundary. The user does not
want to perform routine PR creation, review administration, remediation, or
merge work. The workspace has a clean local `main` baseline but no remote.

## Decision

Use a private GitHub repository. One approved task produces one issue, one
Copilot-owned branch, and one pull request.

The controller will:

1. create or approve the issue and dispatch it to Copilot coding agent;
2. reconcile the branch/PR Copilot creates;
3. attach deterministic gate and independent review evidence;
4. route blocking findings back to Copilot for repair;
5. rerun checks and reviews after every repair;
6. enable policy-controlled auto-merge only when every required status passes.

Protected `main`, required CI statuses, CODEOWNERS, exact base/head checks, and
independent structured review statuses control routine merge. The user is
contacted only for R3/high-risk work, ambiguous requirements, non-converging
remediation, provider/billing failures, or a policy/security incident.

Copilot receives only the repository permissions provided by its managed GitHub
integration. Local Sandcastle workers receive no GitHub write credential. The
host controller uses a fine-grained, repository-scoped user-to-server token for
the issue assignment and PR metadata it must manage; the current broad personal
`gh` token is not used by the daemon. The Copilot assignment/task APIs are
public preview, so their adapter is versioned and fail-closed.

## Consequences

- The user does not need to operate routine PRs.
- PRs remain the auditable integration and quarantine unit.
- Auto-merge depends on machine-created statuses, never agent prose.
- Independent reviews are represented as required checks even if an agent
  cannot submit a formal GitHub approval.
- Local controller state must reconcile with remote issue, branch, PR, check,
  and merge state after restart.
- R3 and escalated work intentionally pauses for explicit user approval.
- Automatic merge is disabled until adversarial controller tests and the first
  documentation-only pilot PR pass.

## Alternatives considered

- Human-managed PR lifecycle: rejected by user preference.
- Agent-driven direct merges: rejected because prompts are not an acceptance
  boundary.
- Local-only branches: cannot provide independent CI and protected integration.
- Copilot self-approval alone: rejected because implementation and acceptance
  must be independent.
