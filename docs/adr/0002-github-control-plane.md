# ADR 0002: Automate PR creation and use Copilot as an additional reviewer

- **Status:** Accepted; Copilot-review decision superseded by ADR 0007
- **Date:** 2026-08-28

## Context

Agent work needs a visible approved queue, immutable integration record,
independent CI, review evidence, and protected merge boundary. The user does not
want to create, administer, or merge routine pull requests. Copilot is available
for GitHub PR review only, while Codex is the coding agent.

## Decision

Use a private GitHub repository. One approved task produces one controller-owned
branch and one pull request.

The controller will:

1. create or approve a task and lease its named branch;
2. run a Codex implementer through Sandcastle;
3. validate the diff, push the branch, and create/update the draft PR;
4. trigger Copilot code review through repository automatic-review settings or
   the GitHub reviewer API;
5. run separate Codex code/security/QA review sessions;
6. route all accepted blocking findings to a fresh Codex remediation session;
7. rerun deterministic checks and all reviews after every repair;
8. enable policy-controlled auto-merge only when every required status passes.

Copilot always leaves a `Comment` review, not `Approve` or `Request changes`, so
its review does not count toward required approvals and does not block merge by
itself. A controller-owned required status records that Copilot reviewed the
current head SHA and that no unresolved Copilot finding remains. Any new push
invalidates that status and triggers re-review.

Protected `main`, required CI/status checks, CODEOWNERS, exact base/head checks,
and independent review statuses control routine merge. The user is contacted
only for R3/high-risk work, ambiguous requirements, non-converging remediation,
provider/billing failures, or a policy/security incident.

The host controller uses a fine-grained repository-scoped credential to push
branches, create PRs, request Copilot review, publish statuses, and enable
eligible auto-merge. Codex/Sandcastle containers receive no GitHub credential.
The current broad personal `gh` token is not used by the daemon.

## Consequences

- The user does not operate routine PRs.
- PRs remain the auditable integration and quarantine unit.
- Auto-merge depends on machine-created statuses, never Codex or Copilot prose.
- Copilot comments require deterministic collection plus bounded remediation;
  unresolved or repeatedly duplicated comments quarantine the PR rather than
  being silently dismissed.
- Local controller state must reconcile with remote branch, PR, checks, review
  threads, and merge state after restart.
- R3 and escalated work intentionally pauses for explicit user approval.
- Automatic merge remains disabled until adversarial controller tests and the
  first documentation-only pilot PR pass.

## Alternatives considered

- Human-managed PR lifecycle: rejected by user preference.
- Copilot coding agent as PR owner: unavailable in the user's setup.
- Agent-driven direct merges: rejected because prompts are not an acceptance
  boundary.
- Treating Copilot review as GitHub approval: technically incorrect; Copilot
  leaves comment-only reviews.
