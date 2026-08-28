# Guarded agent delivery workflow

Status: proposed experiment. Do not enable unattended mutation or automatic
merge until the staged rollout gates in this document have been met.

Related documents:

- [Research and tool comparison](AGENT_ORCHESTRATION_RESEARCH.md)
- [Modern application plan](MODERN_APP_PLAN.md)
- [Engineering quality policy](ENGINEERING_QUALITY_POLICY.md)
- [Policy-as-code enforcement](POLICY_ENFORCEMENT.md)
- [Agent capacity, limits, and unattended operation](AGENT_CAPACITY_POLICY.md)
- [Agent-ready delivery backlog](DELIVERY_BACKLOG.md)

## Intended outcome

An operator approves a detailed backlog. A long-running controller then:

1. finds approved, unblocked tasks;
2. creates or approves a task issue and dispatches it to Copilot coding agent;
3. reconciles the branch and draft PR created by Copilot;
4. runs deterministic build, security, and test gates;
5. asks separate Sandcastle agents for structured code, security, and QA reviews;
6. routes blocking findings back to Copilot through a bounded remediation loop;
7. waits for clean GitHub CI and independent review statuses;
8. enables policy-controlled auto-merge when every required status passes;
9. escalates only R3/high-risk or non-converging work to the user;
10. reconciles merged state and continues with newly unblocked tasks.

The controller should keep operating without conversational prompts. It should
pause individual work or the whole queue when policy says continuing is unsafe.

## Principles

1. **Finite workers, continuous reconciler.** No unbounded agent session.
2. **One task, one branch, one PR.** Scope and ownership are explicit.
3. **Git and CI are authoritative.** Agent statements are evidence, not gates.
4. **Least privilege.** Copilot uses its managed GitHub integration; local
   Sandcastle workers receive no push, merge, issue-close, or production
   credentials.
5. **Independent review.** An implementer cannot approve its own work.
6. **Fail closed.** Missing output, malformed plans, stale base branches, and
   unavailable checks block progress.
7. **Idempotent actions.** Restarting the controller must not duplicate a PR,
   comment, task, or merge.
8. **Evidence over confidence.** Every state transition records machine-readable
   evidence.
9. **Small changes.** Oversized tasks return to planning instead of consuming
   unlimited retries.
10. **Human control remains obvious.** A kill switch and an R3/escalation queue
    are always visible even though routine PRs auto-merge.

## Proposed architecture

```text
GitHub Issues/Project ----dispatch----> Copilot coding agent
(approved task specification)                |
            ^                                v
            |                     branch + draft pull request
            |                                |
+------------------------------------------------------------------+
| Durable controller / reconciler                                  |
| scheduler | leases | budgets | policy | retries | PR coordinator |
+------------------------------------------------------------------+
      |                 |                    |
      |                 |                    +--> GitHub Actions CI
      |                 |                         (no model secrets)
      |                 v
      |          local SQLite + JSONL evidence
      v
Sandcastle adapter
      |
      +--> planner sandbox (read-only structured output)
      +--> code/security reviewer sandboxes (read-only findings)
      +--> QA sandbox (read-only acceptance assessment)
      +--> approved fallback/remediation support (exception path)

Controller --blocking findings--> Copilot remediation on the same PR
Controller --all required statuses green--> GitHub auto-merge
```

### Suggested repository layout

```text
.agent-workflow/
  config.ts                 reviewed policy and budgets
  controller.ts             reconciliation loop
  state.ts                  durable state and migrations
  scheduler.ts              dependencies, leases, concurrency
  policies/
    paths.ts                task/path allowlists and protected files
    risk.ts                 risk classification and required approvals
    gates.ts                required deterministic checks
  providers/
    sandcastle.ts           isolated planner/reviewer/fallback adapter
    github.ts               issues, Copilot dispatch, PRs, checks, auto-merge
  schemas/
    task.ts                 validated task contract
    plan.ts                 planner output
    review.ts               review findings
    evidence.ts             run/gate evidence
  prompts/
    plan.md
    copilot-task.md
    code-review.md
    qa-review.md
    remediate.md
  scripts/
    verify.sh               canonical local deterministic gate
    daemon.sh               foreground service entry point
  state/                    ignored DB, logs, sessions, and artifacts
.github/
  workflows/
    ci.yml
    nightly.yml
  CODEOWNERS
AGENTS.md                    repository-wide agent constraints
docs/
  DELIVERY_BACKLOG.md       approved task DAG
```

Do not create this layout until the proposed ADRs and pilot decisions at the
end of this document are approved and the private remote is protected.

## Durable task contract

Every dispatchable task must validate against a schema containing:

```text
id
parentEpic
objective
nonGoals[]
dependsOn[]
acceptanceCriteria[]
requiredTests[]
allowedPaths[]
protectedPaths[]
riskClass
maxChangedLines
maxAttempts
humanApprovalRequired
```

A task is not dispatchable when its acceptance criteria use vague terms such as
“works well”, when required product behaviour is unresolved, or when it lacks a
path boundary. The planner may propose task changes but cannot approve them.

## State machine

```text
PROPOSED
  -> SPEC_REVIEW
  -> APPROVED
  -> QUEUED
  -> LEASED
  -> COPILOT_DISPATCHED
  -> PR_DRAFT
  -> VERIFYING
  -> REVIEWING
  -> REMEDIATING (bounded loop back to VERIFYING)
  -> CI_PENDING
  -> READY_FOR_POLICY_MERGE
  -> AUTO_MERGE_PENDING
  -> MERGED
  -> DONE
```

Any active state may transition to:

- `BLOCKED_REQUIREMENTS` — acceptance criteria are ambiguous or conflicting;
- `BLOCKED_DEPENDENCY` — an upstream task or external service is unavailable;
- `FAILED_RETRYABLE` — transient provider, network, or sandbox failure;
- `QUARANTINED` — policy violation, repeated failure, suspicious output, secret
  exposure, or non-converging review;
- `CANCELLED` — operator stopped the task;
- `SUPERSEDED` — another PR or plan made the task obsolete.

Each transition stores task ID, attempt, prior/new state, timestamp, base and
head SHA, actor, command/gate identifier, result, and artifact links.

### Leases and recovery

- A task lease has an owner, acquired time, heartbeat, and expiry.
- Only one active lease may exist per task and branch.
- On restart, the controller inspects its DB plus the issue, Copilot assignment,
  remote branch, PR, checks, and head/base SHAs before deciding whether to
  resume, redispatch, or quarantine.
- The controller never duplicates a Copilot assignment or PR for an active task.
- Local Sandcastle fallback worktrees are retained until their evidence and diff
  have been captured.
- The controller never force-pushes a Copilot-owned branch. Unexpected remote
  movement is reconciled or quarantined rather than overwritten.

## Agent roles

### Planner

- Read-only repository and issue access.
- Produces a schema-validated dependency graph and task proposals.
- Cannot modify code, approve tasks, close issues, or choose merge policy.
- A deterministic validator rejects cycles, duplicate IDs, missing acceptance
  criteria, overlapping parallel path claims, and unknown dependencies.

### Copilot implementer and PR owner

- Receives one approved issue/task and only the context needed for it.
- Creates and updates its managed branch and pull request through GitHub.
- May change only task-approved paths and must add tests alongside behaviour.
- Cannot alter merge policy, required statuses, risk class, or acceptance rules.
- Responds to accepted blocking findings on the same PR.
- PR creation or a Copilot completion message is never acceptance.

### Code reviewer

- Starts in a separate agent session.
- Initially has read-only filesystem/tool access.
- Returns structured findings with severity, file/line evidence, rationale,
  required action, and confidence.
- Does not silently edit the implementation branch.

### QA reviewer

- Checks acceptance criteria against the diff, tests, and built application.
- Concentrates on behaviour, regressions, accessibility, persistence, offline
  operation, and target device sizes.
- Produces structured evidence and may request specific new tests.

### Remediation

- The controller sends only accepted findings and the original task contract
  back to Copilot coding agent on the existing PR.
- Copilot may update only the original allowed paths and relevant tests.
- A local Sandcastle remediation/fallback agent is used only when explicitly
  configured and recorded.
- No remediation path may broaden scope or dismiss a high-severity finding.

### Controller

- Is ordinary deterministic code, not another open-ended agent.
- Owns leases, worktrees, policy checks, GitHub writes, and state transitions.
- Opens PRs only after local gates pass.
- Never interprets prose as permission to bypass a failed gate.

## Guardrails

### Repository and path guards

- Keep vendor artifacts ignored and absent from agent worktrees:
  `downloads/`, `extracted/`, `analysis/decrypted/`, signatures, and binaries.
- Treat `CHECKSUMS.txt`, `.gitignore`, workflow policy, lockfiles, and generated
  CI workflows as protected by default.
- Reject changes outside `allowedPaths` before tests or review.
- Reject symlinks escaping the worktree, submodule additions, executable binary
  additions, unexpected generated files, and case-collision paths.
- Enforce a task-specific changed-line and file-count budget.
- Lockfile changes require a task explicitly classified as dependency work.

### Sandbox guards

- Run workers as a non-root user in a fresh container/worktree.
- Never mount the Docker socket, host home, SSH directory, keyring, browser
  profile, or broad project parent directory.
- Mount only the task worktree and read-only caches that have been reviewed.
- Drop Linux capabilities, enable `no-new-privileges`, and set CPU, memory, PID,
  disk, wall-clock, and log-size limits. If Sandcastle's standard provider
  cannot express a required limit, wrap or replace it with a custom provider.
- Build dependencies from the committed lockfile before the agent starts.
- Prefer no outbound network during implementation. If network is necessary,
  use an allowlist and keep credentials out of the worker.
- Destroy successful containers; retain failed worktrees for bounded forensic
  time.

### Secret and GitHub guards

- Do not inject the current broad personal `gh` token into a container.
- Prefer a repository-scoped GitHub App token held by the host controller.
- Workers should receive sanitized issue/task data, not GitHub write access.
- CI for PR code receives no deployment or model credentials.
- Pin third-party Actions to reviewed commit SHAs, use minimal job permissions,
  and enable secret scanning where available.
- Never use `pull_request_target` to check out and execute untrusted PR code.
  Mutating follow-up jobs must consume validated artifacts or trusted refs.

### Prompt-injection guards

- Only owner-approved issues with an explicit automation label enter the queue.
- Treat issue bodies, PR comments, source comments, imported JSON, and web
  content as untrusted data, not instructions.
- Pass untrusted content through inert structured arguments.
- Never expand shell commands originating in issue or review text.
- Only explicit commands from an allowlisted maintainer may trigger a repair or
  retry.

### Circuit breakers

Pause one task when any of these occurs:

- two failed implementation attempts;
- two review/remediation cycles without convergence;
- protected-path or scope violation;
- malformed structured output after one schema repair attempt;
- stale branch or unexpected remote commit;
- suspected credential or proprietary-data access;
- unexplained test deletion or material coverage reduction.

Pause all dispatch when any of these occurs:

- three infrastructure failures in ten minutes;
- provider spend exceeds the daily budget;
- main branch CI is red;
- sandbox isolation preflight fails;
- GitHub permissions or branch protection differ from policy;
- disk, memory, or API capacity crosses configured limits.

## Deterministic quality gates

`ENGINEERING_QUALITY_POLICY.md` is authoritative for code standards, prohibited
shortcuts, compiler/linter configuration, architecture boundaries, test quality,
security checks, review schemas, remediation, and protected quality files.
`POLICY_ENFORCEMENT.md` defines how trusted controller code, Sandcastle
sandboxes, deterministic statuses, and GitHub protection enforce that policy
without relying on an agent reading or obeying documentation.

The canonical `verify` command must run identically in the agent sandbox and CI.
For the React/TypeScript PWA it should eventually include:

### Every commit/PR

1. clean install from the lockfile;
2. formatting check;
3. ESLint with zero warnings;
4. TypeScript typecheck with no emit;
5. unit and domain tests;
6. production Vite build;
7. path/scope and generated-artifact checks;
8. dependency and secret scan appropriate to changed files.

### Domain-engine changes

- fake-clock tests for pause/resume, delayed callbacks, sleep gaps, and exactly
  once level transitions;
- money and chip reconciliation tests;
- serialization round-trip and schema migration tests;
- property tests for seating uniqueness, capacity, locks, unavailable seats,
  balancing, consolidation, and seed repeatability;
- command undo/inverse tests.

### UI, persistence, or PWA changes

- Playwright journeys at desktop, tablet, and phone viewports;
- accessibility scan and keyboard-only critical path;
- refresh/recovery and IndexedDB failure paths;
- offline shell/load test;
- Director/Display read-only boundary test;
- visual snapshots only for stable, high-value screens.

### Scheduled/nightly gates

- broader browser matrix;
- mutation tests for the domain engine once unit coverage is stable;
- dependency vulnerability and license report;
- backup import fuzz/property tests;
- full multi-table tournament simulation;
- flake detection by repeating timing-sensitive and end-to-end tests.

Coverage is a regression signal, not proof. Start thresholds only after a
baseline exists, then prevent changed-code coverage from falling.

## Review and PR policy

1. Controller dispatches one approved issue to Copilot coding agent.
2. Copilot creates the task branch and draft PR.
3. Controller checks the diff/path policy and GitHub CI runs `verify` cleanly.
4. Read-only code, security, and QA reviews run in separate Sandcastle sessions.
5. Controller validates findings and sends blocking items back to Copilot for at
   most two remediation rounds on the same PR.
6. All deterministic gates and independent reviews rerun after every repair.
7. Controller maintains one evidence/status comment rather than noisy logs.
8. PR becomes policy-ready only when required checks pass, no critical/high or
   unexcepted medium findings remain, and the head/base SHAs are current.
9. R0-R2 PRs enter GitHub auto-merge; R3/escalated PRs wait for the user.
10. The task closes only after merge and a green `main` build.

Do not let a reviewer agent both approve and merge. Do not have a merger agent
resolve several unrelated branches directly on main.

### Risk classes

| Class | Typical change | Pilot policy |
|---|---|---|
| R0 | Documentation only, no workflow/security policy | Copilot PR; deterministic checks and independent review status; policy auto-merge |
| R1 | Tests, isolated UI, non-critical refactor | Copilot implementation/remediation; full CI/review statuses; policy auto-merge |
| R2 | Domain logic, persistence, money, clock, seating | Copilot implementation; code, security, and QA reviews; full CI; policy auto-merge |
| R3 | CI/workflows, dependencies, imports, auth/secrets, release config | Copilot may propose; explicit user approval; never auto-merge |

Auto-merge remains disabled globally until the adversarial controller suite and
first documentation-only pilot PR pass. It is then enabled by policy for R0,
followed by R1 and R2 only after each class meets its staged exit criteria.

## Scheduling, capacity, and parallelism

`AGENT_CAPACITY_POLICY.md` is authoritative for provider limits, spend and run
budgets, review reserves, durable cooldowns, backoff, session/worktree recovery,
provider fallback, and behaviour while all models are unavailable. Sandcastle
fails fast on provider errors; the controller must not blindly retry them.

- Start with one implementation at a time.
- Increase to two only after ten clean pilot PRs.
- Parallel tasks must have satisfied dependencies and non-overlapping primary
  path claims.
- Clock, transaction, undo, serialization, and seating core changes are treated
  as one conflict domain even when files differ.
- Reserve capacity for one review/repair job so implementers cannot starve QA.
- Use weighted budgets per risk class and task size rather than an unlimited
  backlog drain.

## Observability

Maintain a concise status command/dashboard showing:

- controller health and policy version;
- queue, active leases, retries, blocked and quarantined tasks;
- task/branch/PR/base/head SHA mapping;
- current agent role, provider/model, elapsed time, and budget;
- latest deterministic gate and review result;
- daily token/cost totals and failure rates;
- kill switch state.

Store prompts, resolved task contracts, structured outputs, command exit codes,
diffs, check summaries, and provider usage. Redact secrets and cap retention.
The audit record must be sufficient to explain why a PR became ready.

## Continuous operation

Run the controller in the foreground during early pilots. After it survives
restart and recovery tests, supervise it with a user service or dedicated
container:

- automatic restart with backoff;
- one controller instance enforced by a file/database lock;
- graceful shutdown cancels workers and preserves worktrees;
- startup performs environment, Git, remote, branch-protection, disk, Docker,
  and credential preflights;
- a periodic reconcile handles PR comments/checks without relying only on
  webhooks;
- an operator file/label provides an immediate global pause.

“Always on” should be implemented by the supervisor and durable state—not a
shell `while true` around an agent prompt.

## Staged experiment

### Stage 0 — prerequisites and threat model

- Local Git and a clean baseline commit are complete; create a private remote.
- Reconfirm ignored proprietary files are absent before the first push.
- Establish `main`, branch protection, CODEOWNERS, and required checks.
- Fix Docker access without weakening socket permissions broadly.
- Choose agent provider/model routes, authentication, concurrency, review
  reserve, hard run/spend budgets, fallback policy, and log retention.
- Approve this threat model and the kill-switch procedure.

Exit: a human can run clean CI and an isolated no-op worker without exposing
host credentials or vendor artifacts.

### Stage 1 — read-only planning

- Implement task and plan schemas.
- Encode and test the quality policy, including deliberately bad fixture diffs
  that every expected guard must reject.
- Feed `DELIVERY_BACKLOG.md` to a planner in a sandbox.
- Compare the emitted DAG and path claims with the reviewed backlog.
- Exercise malformed output, cycles, overlap, and prompt-injection fixtures.

Exit: ten planner runs produce valid proposals and no repository mutations.

### Stage 2 — documentation-only PR

- Implement leases, Copilot dispatch/reconciliation, evidence capture, path
  guard, required statuses, and policy auto-merge control.
- Permit only an R0 documentation task.
- Restart the controller during execution and verify idempotent recovery.

Exit: Copilot creates one PR, checks and independent review status pass,
auto-merge occurs once, and no routine user action is needed.

### Stage 3 — code pilot

- Add canonical verification, implement/review/QA/remediation roles, retries,
  budgets, and circuit breakers.
- Process five small R1 tasks sequentially.
- Deliberately inject a test failure, malformed review, stale branch, protected
  path edit, and provider timeout.

Exit: unsafe cases quarantine correctly; successful cases need no manual state
repair.

### Stage 4 — bounded parallel operation

- Add dependency-aware scheduling and two-worker concurrency.
- Run independent R1 tasks and one R2 task with two reviews.
- Measure cost, cycle time, review usefulness, CI mismatch, and flake rate.

Exit: at least ten further PRs with no scope escape, accidental duplicate,
credential incident, or direct-main mutation.

### Stage 5 — supervised daemon

- Add process supervision, startup preflight, health status, global pause,
  retention cleanup, and daily budget reset.
- Keep R3/escalation approval enabled while routine R0-R2 work uses policy
  auto-merge.

Exit: one week of operation with successful restart recovery and acceptable
cost/quality metrics.

## Success metrics

Track these rather than raw agent activity:

- accepted PRs / attempted tasks;
- first-pass and final CI pass rates;
- escaped defects and reverted PRs;
- user escalations and policy exceptions per PR;
- review findings accepted/rejected;
- task scope violations and quarantines;
- retries and non-converging repair loops;
- median cycle time and cost per accepted PR;
- duplicate PR/state-recovery incidents;
- flaky test rate.

## Decisions required before implementation

1. Where should the private GitHub remote live?
2. Which Sandcastle model/provider should perform independent reviews?
3. Which credentials and monthly/daily spend limit are acceptable?
4. Should the controller run only on this machine or on a dedicated runner?
5. How long should failed worktrees, prompts, and model transcripts be retained?

Accepted defaults: Copilot coding agent owns routine implementation and PRs;
R0-R2 may policy-auto-merge after their staged gates; R3 and escalations require
the user. Remaining recommendation: private GitHub repository, one active agent
run at a time, explicit review reserve and hard budget, and 14-day failed-run
retention.
