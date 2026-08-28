# Agent capacity, limits, and unattended operation

Status: required design for the orchestration pilot.

This policy covers model rate limits, account/session quotas, context limits,
provider outages, spend budgets, and what “unattended” means when no model is
currently available.

Related documents:

- [Guarded agent delivery workflow](AGENT_ORCHESTRATION_PLAN.md)
- [Policy-as-code enforcement](POLICY_ENFORCEMENT.md)
- [Engineering quality policy](ENGINEERING_QUALITY_POLICY.md)

## Sandcastle behaviour

Sandcastle deliberately does **not** retry provider errors such as rate limits,
authentication failures, quota exhaustion, or network timeouts. It launches
provider CLIs but does not own their API protocol, and blindly retrying a
non-zero exit could hide a real error or waste more quota. A provider failure
therefore ends that Sandcastle planning/review/fallback run and is returned to
our harness.

Copilot coding agent runs through GitHub rather than Sandcastle. The controller
observes its issue assignment, branch, PR, checks, comments, and timeout state.
Copilot implementation capacity and Sandcastle review-provider capacity are
tracked separately; exhaustion of one must not be mistaken for exhaustion of
the other.

Sandcastle can expose raw token usage when the provider stream/session contains
it. It does not know the user's total account allowance, reliable remaining
percentage, billing state, or reset time. Those limits differ by provider,
model, and subscription and may not have a stable machine-readable API.

Therefore capacity handling belongs to our controller around Sandcastle.

## Core rule

An unavailable model pauses model work; it does not lose repository work and it
does not cause a retry storm.

The always-on component is the durable controller. Individual agents remain
finite and disposable. If Codex is unavailable for six hours, the controller
may spend six hours safely waiting while it continues to reconcile CI, PRs,
leases, disk usage, and operator status.

“Unattended” means no person must keep a terminal conversation open. It does not
mean the system must continuously consume tokens or bypass a provider limit.
On this local machine it works only while the machine, network, Docker, and
supervised controller are running. Sleep or shutdown pauses execution; durable
state allows reconciliation to resume after boot. A dedicated runner would be
needed for true 24/7 operation.

## Capacity state

Track each configured provider/model independently:

```text
UNKNOWN
AVAILABLE
DEGRADED
COOLDOWN_RATE_LIMIT
COOLDOWN_QUOTA
BLOCKED_AUTH
BLOCKED_BILLING
BLOCKED_POLICY
DISABLED_BY_OPERATOR
```

Each state records:

```text
provider
model
observedAt
source: structured-event | exit | stderr | operator | configured-window
retryAt?
consecutiveFailures
lastSuccessfulRunAt?
usageObserved?
reasonCode
redactedEvidence
```

Task states add:

```text
WAITING_CAPACITY
WAITING_BUDGET
WAITING_REVIEW_CAPACITY
```

A confirmed provider limit does not consume an implementation-attempt retry.
A code/test failure does.

## Failure classification

The provider adapter—not an agent prompt—classifies a failed run.

| Failure | Controller action |
|---|---|
| Structured rate limit with valid retry/reset time | Set provider cooldown until that time plus small jitter |
| Recognised quota/usage cap without reset time | Long cooldown and operator notice; conservative scheduled probe |
| Authentication failure | Block provider; no automatic retry |
| Billing/plan failure | Block provider; no automatic retry |
| Transient network/5xx failure | At most two retries with exponential backoff and jitter |
| Agent idle timeout | Preserve work, retry once only if provider health is good |
| Context exhaustion | Stop that session; create a compact checkpoint and start a fresh session if task budget permits |
| Unknown non-zero exit | Do not guess; quarantine run or request operator classification |
| Quality/test failure | Not a capacity event; enter normal remediation policy |

Parsing provider CLI output is inherently brittle. Prefer structured stream
events and explicit exit data. Store a versioned classifier fixture for every
recognised provider error. Unknown formats fail closed rather than being treated
as rate limits.

Never loop immediately on `429`, “usage limit reached”, authentication errors,
or an unknown failure.

## Admission control

Before starting a model run, the scheduler checks all of:

- provider/model state is `AVAILABLE` or explicitly probeable;
- current time is after `retryAt`;
- provider concurrency semaphore has capacity;
- task and role budgets have remaining allowance;
- daily/weekly configured soft and hard budgets are not exceeded;
- reserved review/remediation capacity remains available;
- main CI, sandbox preflight, disk, and global circuit breakers are healthy.

The scheduler atomically reserves capacity before launching the sandbox. A
restart reconstructs reservations from durable active leases and process state.
Stale reservations expire safely.

### Initial conservative settings

For the pilot:

- maximum one Copilot implementation task in flight at a time;
- maximum one Sandcastle planning/review model run at a time until usage is
  understood;
- track Copilot and each Sandcastle provider in separate capacity ledgers;
- reserve at least 30% of the configured Sandcastle allowance for
  code/security/QA review and remediation;
- stop new implementations at the soft budget threshold;
- permit already-started verification/review to use only its reserved budget;
- stop all model dispatch at the hard threshold;
- no automatic provider failover;
- no blind provider-error retry;
- no periodic planner call when the backlog has not changed.

The actual cash/request/token thresholds must be chosen by the user before the
pilot. When subscription quotas are opaque, use observed local usage as a
conservative signal and rely on provider errors as the hard stop.

## Work preservation when a limit is hit

For Copilot implementation, committed progress is already on its managed remote
branch/PR. If Copilot stops mid-task, the controller records the current PR head
SHA and diff, leaves the PR draft, marks the task `WAITING_CAPACITY`, and later
asks Copilot to continue on that same PR. It never creates a duplicate issue,
branch, or PR.

For local Sandcastle planning/review/fallback, the bind-mounted Git worktree
survives the agent process and container. On a limit:

1. cancel/finish the Sandcastle run;
2. stop its timeout and capacity reservation;
3. record redacted provider evidence and any usage reported;
4. inspect the worktree using the trusted diff/path guard;
5. preserve existing commits and uncommitted diff locally;
6. never mark the task complete or open a ready PR;
7. close the container to release CPU/memory;
8. retain the task lease in `WAITING_CAPACITY`, with a long heartbeat/expiry;
9. schedule a wake-up at `retryAt` or await operator action.

If Sandcastle captured a valid resumable Codex/Claude/Pi session, the controller
may resume it after capacity returns. If not, a fresh agent receives the
original task, current branch diff, completed checks, and a compact factual
checkpoint. Git state, not conversation memory, is authoritative.

A second agent is never launched concurrently on the same task branch.

### Exact mid-run interruption transaction

The controller wraps every Sandcastle call in a durable run transaction:

1. **Before launch:** persist run ID, task lease, provider/model, branch, exact
   base/head SHA, worktree path, policy hash, budget reservation, and state
   `IMPLEMENTING`.
2. **During launch:** heartbeat the run from the controller process. Agent prose
   is streamed to bounded logs but does not drive state transitions.
3. **On provider exit/error:** catch Sandcastle's failure and immediately mark
   the run `INTERRUPTED_UNCLASSIFIED`; this survives a controller crash during
   classification.
4. **Quiesce:** ensure the agent subprocess and container have stopped, release
   the capacity reservation, and prevent any other worker claiming the branch.
5. **Inventory Git:** from trusted host code record `HEAD`, status, commits since
   base, changed paths, diff hash, and whether uncommitted files exist. Run the
   protected-path/secret/symlink guard, but do not expect the incomplete build
   or tests to pass.
6. **Preserve:** committed changes remain on the named task branch even if a
   clean worktree is removed. Sandcastle preserves a dirty worktree and reports
   its path; the controller also stores that path and a hashed patch/evidence
   artifact. It does not auto-commit unknown partial changes.
7. **Classify:** only now change state to `WAITING_CAPACITY`, `BLOCKED_AUTH`,
   `BLOCKED_BILLING`, infrastructure retry, or quarantine.
8. **Schedule:** persist `retryAt` and close expensive resources. Cooldown state
   is independent of the process, so reboot does not cause an early retry.
9. **Resume:** reacquire the same lease, verify branch/worktree/diff hashes and
   remote SHA have not changed, then reopen the existing named branch. Resume a
   captured provider session if valid; otherwise start a fresh session with a
   factual checkpoint.
10. **Re-verify:** the resumed agent must finish the original acceptance
    criteria. Full quality gates and independent reviews run from the beginning;
    work completed before interruption receives no exemption.

A provider interruption has its own counter and does not consume a code-quality
attempt. Repeated capacity interruptions still trip a provider circuit breaker
so a permanently exhausted account cannot wake and fail forever.

### Expired credits versus temporary limits

The controller cannot create capacity that the account no longer has:

- a rate limit or known allowance reset enters timed cooldown and may resume
  automatically;
- exhausted credits with a documented refill date wait until that date;
- expired subscription, disabled billing, or no remaining credits enters
  `BLOCKED_BILLING` with no automatic retry;
- an unclear “limit reached” response uses a conservative cooldown and one
  probe, then asks the operator rather than guessing.

While blocked, all local Git work remains available. The task resumes only after
the operator restores that provider or explicitly approves a configured
fallback.

## Context-window handling

Account quota and context-window exhaustion are different problems.

- Keep one task small enough for one focused context.
- Supply only task-relevant architecture and files.
- Use structured checkpoints rather than repeatedly injecting full transcripts.
- Record raw usage when available, but do not infer a percentage without a
  reliable model context size.
- Before a planned session handoff, require a checkpoint containing changed
  files, commands/results, unresolved acceptance criteria, and current SHA.
- If context exhaustion happens mid-task, start one fresh session only when the
  task remains within its attempt and budget limits; otherwise split or
  quarantine the task.

Do not solve context pressure by dropping security/quality instructions. The
controller still enforces them even when prompt context is compacted.

## Provider fallback

Multiple installed CLIs do not automatically create safe interchangeable
capacity. Models differ in behaviour, authentication, cost, session formats,
and review independence.

A fallback route must be pre-approved, for example:

```text
role: independent-review
primary: codex / approved model
fallback: claude-code / approved model
maxFallbackRuns: 1
requiresFreshSession: true
```

Rules:

- never silently switch provider/model;
- show provider/model on every run and PR evidence record;
- do not resume one provider's private session with another provider;
- hand off through task contract, Git diff, test evidence, and checkpoint;
- fallback uses its own concurrency and spend budget;
- R2/R3 fallback requires the same or stronger review policy;
- credentials must already be configured and purpose-limited;
- if all approved providers are unavailable, wait.

A useful later configuration may use a lower-cost model for planning and routine
R0 review while preserving a stronger model and quota reserve for R2 domain
work. This should follow measured pilot results, not assumptions.

## Unattended reconciliation loop

The controller should be event/timer driven, not a busy shell loop:

```text
reconcile durable leases
reconcile branches and PR checks
release stale capacity reservations
wake providers whose cooldown expired
admit eligible work within capacity/budget
sleep until next event, retryAt, webhook, or health interval
```

While models are limited, it can still:

- observe GitHub CI and PR state;
- run deterministic local checks that do not need a model;
- update one status record/comment;
- clean expired containers/artifacts according to retention policy;
- detect remote branch changes;
- preserve and report blocked tasks;
- accept a global pause/resume command.

It must not repeatedly launch “probe” agents. Use a lightweight provider health
probe only when the CLI/provider has a documented low-cost mechanism; otherwise
the next scheduled eligible run is the probe, with increasing cooldown after
failure.

## Backoff defaults

When a retry time is not supplied and the failure is confidently transient:

```text
network/5xx: 1 minute, 5 minutes; then provider degraded
rate limit: 15 minutes, 30 minutes, 1 hour, 2 hours, maximum 4 hours
opaque quota: 4 hours, then one probe; repeated failure requires operator review
```

Add random jitter so concurrent workers do not restart together. Persist
`retryAt`; a controller restart must not reset the delay.

Authentication and billing failures have no timed retry. They require an
operator to re-enable the provider after correction.

## Budget ledger

Record per run when available:

- provider and exact model;
- role and task;
- started/ended time;
- raw input, cached input, and output token usage;
- request/run count;
- known monetary cost or `unknown`;
- result and capacity classification.

Never invent a monetary estimate when subscription/API pricing is unknown.
Support hard controls that remain meaningful without price data: runs per hour,
runs per day, maximum iterations, wall-clock duration, concurrency, and reserved
review slots.

Budget decisions are made before dispatch. An agent cannot request more budget
through prose.

## Operator visibility and alerts

The status view should answer:

- Is the controller alive?
- Which providers/models are available, cooling down, or blocked?
- When is the next eligible retry?
- Which tasks are safely waiting and what Git SHA/worktree holds their work?
- How much configured budget and review reserve remains?
- Did a task stop because of capacity, code quality, infrastructure, or policy?
- Is fallback enabled, and will it cost money?

Notify the operator on first provider exhaustion, auth/billing failure, all
providers unavailable, hard budget reached, or a task waiting longer than the
configured service target. Repeated cooldown logs should be coalesced.

## Required tests

Before unattended operation, simulate:

1. Codex rate limit with a valid reset time;
2. opaque usage-limit text with no reset time;
3. authentication and billing failures;
4. two transient network failures followed by recovery;
5. unknown provider exit format;
6. controller restart during cooldown;
7. partial committed and uncommitted work at limit time;
8. capacity returning with and without a resumable session;
9. all configured providers unavailable;
10. review reserve preventing an implementer from consuming the final budget;
11. stale task lease during a long cooldown;
12. fallback disabled and explicitly enabled;
13. daily hard budget reached;
14. output flood/idle timeout mistaken for a provider limit;
15. repeated probes proving exponential backoff survives restart.

Tests use fake time and fake provider adapters. They must not consume real model
quota.

## Pilot expectation

The safest initial expectation is not “agents work every minute”. It is:

- approved work proceeds unattended while capacity and policy permit;
- provider limits cause a safe, durable pause;
- no progress or evidence is lost;
- no duplicate agents or retry storms appear;
- review capacity is preserved;
- work resumes automatically when a reliable reset time/cooldown permits;
- ambiguous quota/auth/billing failures wait for a person;
- no R0-R2 merge occurs without independent CI and required review statuses;
- R3/escalated work does not merge without explicit user approval.

This still provides the main unattended benefit: the controller manages starts,
stops, waiting, recovery, checks, reviews, and PR state without requiring a
person to supervise each agent conversation.

## Sandcastle sources

- Provider retry decision:
  <https://github.com/mattpocock/sandcastle/blob/main/.out-of-scope/provider-error-retry.md>
- Raw usage/context-window decision:
  <https://github.com/mattpocock/sandcastle/blob/main/docs/adr/0005-usage-raw-tokens-no-percentage.md>
- Run options, session capture/resume, and usage fields:
  <https://github.com/mattpocock/sandcastle#readme>
