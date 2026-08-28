# ADR 0004: Use conservative provider capacity and routing

- **Status:** Proposed
- **Date:** 2026-08-28

## Context

Codex is the only coding agent and must cover implementation, remediation,
planning, and fresh-session reviews. It can hit request, account, context,
credit, or billing limits during any role. Sandcastle fails fast and
deliberately does not retry provider errors. Subscription capacity may not
expose a reliable remaining quota or reset API. Copilot is a separate PR-review
integration, not fallback coding capacity.

## Proposed decision

The durable controller owns provider state, admission, cooldowns, budgets, and
recovery. Start with one Codex/Sandcastle run at a time, 30% of the shared Codex
allowance reserved for review/remediation, no blind retries, and no automatic
provider fallback.

A mid-task Sandcastle interruption preserves the named branch and dirty
worktree, enters durable `WAITING_CAPACITY`, and resumes only after a reliable
reset, conservative cooldown, or explicit operator action. Auth/billing
failures never retry automatically. Copilot review timeout/failure pauses the PR
review status but is not treated as Codex capacity.

## Consequences

- Unattended work pauses safely rather than being continuous at all costs.
- Git state and task evidence, not conversation memory, enable recovery.
- Throughput is intentionally low until real usage and failure data exists.
- A future fallback provider requires explicit model, budget, credential, and
  review configuration.
- True 24/7 operation requires a dedicated always-on runner; a local machine
  pauses during sleep or shutdown.

## Alternatives considered

- Immediate retry on failure: risks quota storms and misclassifying auth or code
  failures.
- Automatic cross-provider fallback: obscures cost and behaviour during the
  pilot.
- Unlimited concurrency: can consume Codex implementation quota before the
  required review/remediation work completes.

## Approval needed

Confirm the Codex model/reasoning levels by role, hard run/spend limits, 30%
review reserve, fallback policy, and local versus dedicated execution.
