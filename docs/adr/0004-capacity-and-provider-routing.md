# ADR 0004: Use conservative provider capacity and routing

- **Status:** Proposed
- **Date:** 2026-08-28

## Context

Codex and other providers can hit request, account, context, credit, or billing
limits during a task. Sandcastle fails fast and deliberately does not retry
provider errors. Subscription capacity may not expose a reliable remaining
quota or reset API.

## Proposed decision

The durable controller owns provider state, admission, cooldowns, budgets, and
recovery. Start with one model run at a time, Codex as the proposed primary,
30% capacity reserved for review/remediation, no blind retries, and no automatic
provider fallback.

A mid-task provider interruption preserves the named branch and dirty worktree,
enters durable `WAITING_CAPACITY`, and resumes only after a reliable reset,
conservative cooldown, or explicit operator action. Auth/billing failures never
retry automatically.

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
- Unlimited concurrency: can consume implementation quota before review.

## Approval needed

Confirm the primary provider/model, hard run/spend limits, 30% review reserve,
fallback policy, and local versus dedicated execution.
