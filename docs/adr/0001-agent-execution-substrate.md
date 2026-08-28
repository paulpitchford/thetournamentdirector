# ADR 0001: Use Copilot for implementation and Sandcastle for independent agent roles

- **Status:** Accepted
- **Date:** 2026-08-28

## Context

The user has GitHub Copilot coding agent and does not want to create, maintain,
review, or merge routine pull requests. The experiment still needs isolated
planning/review agents, provider flexibility, bounded runs, structured outputs,
and an independent quality path.

## Decision

Use GitHub Copilot coding agent as the primary implementer and PR owner. The
controller creates or approves a task issue, dispatches it to Copilot, and then
reconciles the branch and PR that Copilot creates.

Use `@ai-hero/sandcastle` as a replaceable execution adapter for planning,
independent code/security/QA review, controlled remediation support, and an
explicitly configured fallback—not as the routine PR author or merge authority.

The deterministic controller owns task state, capacity, policy checks, review
status, remediation routing, and merge eligibility. GitHub Actions and branch
rules remain authoritative.

For any local Sandcastle work, use explicit named branches. Do not use
unattended `head`, `merge-to-head`, or `noSandbox()` execution.

## Consequences

- Routine implementation and PR mechanics use the existing Copilot entitlement.
- Sandcastle reviews remain independent of the Copilot implementer.
- The controller needs a Copilot/GitHub dispatch adapter and a Sandcastle review
  adapter; Copilot agent-task/assignment APIs are public preview and must be
  isolated behind a versioned interface.
- Copilot completion or PR creation never implies quality approval.
- Agent responsibilities, provider usage, and review evidence remain visible on
  every PR.
- Sandcastle remains replaceable if the pilot exposes security, reliability, or
  maintenance problems.
- A hardened sandbox provider may still be needed for local review/fallback
  agents.

## Alternatives considered

- Sandcastle implementers for every task: flexible, but duplicates an available
  Copilot coding-agent workflow and consumes separate model capacity.
- Copilot for implementation and self-review: rejected because implementation
  and acceptance would not be independent.
- Gas Town as the whole control plane: more complete, but much heavier for one
  application and less suited to a small measured pilot.
- Build all provider plumbing ourselves: maximum control but unnecessary scope.
