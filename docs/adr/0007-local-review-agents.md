# ADR 0007: Use separate local Codex review and QA agents

- **Status:** Accepted
- **Date:** 2026-08-28
- **Supersedes:** the Copilot-review portions of ADR 0001 and ADR 0002

## Context

Copilot PR review consumes a separate quota and can prevent otherwise safe work
from progressing. The user does not want model reviews to execute on GitHub or
to add review-service spend. Codex is the available coding agent and can run
locally through the guarded runner.

Using the implementation session to review its own work would preserve context
bias and is not an independent role. Code/security review and QA also have
different objectives and evidence requirements.

## Decision

Run all model review locally. Every candidate head SHA requires:

1. a fresh, read-only Codex code/security reviewer;
2. a separate fresh Codex QA agent that maps acceptance criteria to test and
   runtime evidence.

The implementation, code/security, QA, and remediation roles must use distinct
sessions. The controller rejects missing/reused session identities and malformed
structured evidence. A remediation push invalidates both reviews and requires
new sessions against the new head SHA.

Do not request Copilot review and do not run a model in GitHub Actions. GitHub
stores the PR, deterministic CI, and controller-published `review/code-security`
and `review/qa` statuses. GitHub Actions may continue to execute non-model,
deterministic checks on clean hosted runners.

## Consequences

- Copilot quota and review availability no longer block delivery.
- Review and QA consume the shared Codex allowance, making the 30% reserve
  mandatory.
- Fresh sessions provide role/context separation but not model/provider
  diversity.
- Local review workers receive no GitHub credential and cannot publish their own
  passing status.
- The trusted controller must bind structured review evidence to the exact task,
  base SHA, and head SHA before publishing status.
- R3, ambiguous, malformed, or non-converging work still escalates to the user.

## Alternatives considered

- Copilot as an additional required reviewer: rejected because it adds a quota
  dependency the user does not want.
- Implementation-session self-review: rejected because it is not independent.
- Review agents running in GitHub Actions: rejected because model execution and
  credentials must remain local.
- No model review: rejected because deterministic tests do not assess every
  code-quality, security, or acceptance concern.
