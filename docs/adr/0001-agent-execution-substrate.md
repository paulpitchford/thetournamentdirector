# ADR 0001: Use Sandcastle as the agent execution substrate

- **Status:** Proposed
- **Date:** 2026-08-28

## Context

The experiment needs isolated worktrees, bounded coding-agent runs, provider
adapters, logs, structured output, session capture, and explicit branches.
Sandcastle supplies these primitives in TypeScript but intentionally does not
provide our durable queue, quality policy, provider retry policy, or PR
governance.

## Proposed decision

Use `@ai-hero/sandcastle` as a replaceable execution adapter. Build a thin,
repository-specific controller for leases, capacity, policy checks, reviews,
remediation, GitHub state, and recovery.

Use explicit named branches only. Do not use unattended `head`,
`merge-to-head`, or `noSandbox()` execution.

## Consequences

- We reuse maintained sandbox and agent-provider plumbing.
- The controller remains responsible for acceptance and durable state.
- Sandcastle completion signals and commits never imply quality approval.
- The adapter must be replaceable if the pilot exposes unacceptable security,
  reliability, or maintenance problems.
- A hardened sandbox provider or wrapper may be needed beyond the standard
  Docker provider.

## Alternatives considered

- Gas Town as the whole control plane: more complete, but much heavier for one
  application and less suited to a small measured pilot.
- GitHub Agentic Workflows only: useful later for safe GitHub automation, but
  not the preferred local durable build controller.
- Build all sandbox/provider plumbing ourselves: maximum control but unnecessary
  initial scope and risk.

## Approval needed

Confirm Sandcastle should be the first execution substrate rather than running
a comparative Sandcastle/Gas Town spike.
