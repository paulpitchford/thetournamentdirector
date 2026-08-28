# ADR 0004: Use conservative provider capacity and routing

- **Status:** Proposed
- **Date:** 2026-08-28

## Context

Copilot coding agent and Sandcastle review providers such as Codex can each hit
request, account, context, credit, or billing limits during a task. Sandcastle
fails fast and deliberately does not retry provider errors. Subscription
capacity may not expose a reliable remaining quota or reset API.

## Proposed decision

The durable controller owns provider state, admission, cooldowns, budgets, and
recovery. Track Copilot implementation capacity separately from each Sandcastle
review provider. Start with one Copilot task in flight, one Sandcastle model run
at a time, Codex as the proposed independent reviewer, 30% of Sandcastle
capacity reserved for review/remediation, no blind retries, and no automatic
provider fallback.

A mid-task Copilot interruption preserves its remote branch/PR. A local
Sandcastle interruption preserves the named branch and dirty worktree. Both
enter durable `WAITING_CAPACITY` and resume only after a reliable reset,
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
- Unlimited concurrency: can consume Copilot or review capacity before the
  required acceptance work completes.

## Approval needed

Confirm the Sandcastle review provider/model, separate Copilot/review hard
limits, 30% review reserve, fallback policy, and local versus dedicated review
execution.
