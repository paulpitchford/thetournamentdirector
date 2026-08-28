# Policy-as-code enforcement

Status: required design for the orchestration pilot.

This document answers a specific concern: quality and security must not depend
on whether an agent happened to read or follow a Markdown file.

The documents explain intent to people and agents. The controller, sandbox,
checks, and GitHub rules enforce it. An agent may ignore every instruction and
still must be unable to change `main`, weaken the checks, access credentials, or
progress a non-compliant diff beyond a quarantined branch.

Related documents:

- [Guarded agent delivery workflow](AGENT_ORCHESTRATION_PLAN.md)
- [Engineering quality policy](ENGINEERING_QUALITY_POLICY.md)
- [Agent capacity, limits, and unattended operation](AGENT_CAPACITY_POLICY.md)
- [Delivery backlog](DELIVERY_BACKLOG.md)

## What Sandcastle does

Sandcastle is a TypeScript execution library. A typical run:

1. creates or reuses a Git worktree on an explicit branch;
2. starts a Docker/Podman/remote sandbox containing that worktree;
3. launches the selected coding-agent CLI with a trusted prompt;
4. streams and records agent output;
5. enforces iteration, idle, completion, cancellation, and lifecycle limits;
6. captures commits and optional agent sessions;
7. returns the branch, commits, output, and logs to the caller;
8. closes the sandbox when requested.

It also supports schema-validated structured output and a reusable sandbox whose
`exec()` method can run build/test commands between agent runs.

Sandcastle does **not** know this project's coding standards and does not decide
that code is good. Its completion signal means only “stop this agent loop”. Its
commit list means only “the agent created commits”. Neither means “accept the
change”. The repository-specific controller must make that decision.

## Trust model

Treat every agent like an unreliable external contributor.

Copilot coding agent is allowed through its managed GitHub integration to:

- receive one approved task issue;
- create and update its task branch and PR;
- edit task-approved paths and run repository development tools;
- respond to accepted review findings on the same PR.

Copilot cannot merge, alter branch protection, manufacture required statuses,
change task risk/acceptance policy, or approve its own result.

Local Sandcastle planning/review/fallback agents are allowed to read one clean
worktree, run approved tools, and return structured output. They receive no
GitHub write token, SSH key, Docker socket, or host credentials.

No agent is allowed to:

- access ignored vendor artifacts or the parent project directory;
- edit policy, CI, package, lock, compiler, lint, or test configuration in a
  normal feature task;
- decide that a failed check can be ignored;
- expand its own allowed paths, budget, risk class, or acceptance criteria.

The controller itself runs from a trusted `main` checkout or separately
installed package. It must never load controller/policy code from the agent's
branch before deciding that branch is safe.

## Three independent enforcement planes

### 1. Capability enforcement

The local Sandcastle sandbox limits what its planner/reviewer/fallback worker can
physically do:

- separate worktree and container;
- non-root user;
- only the worktree mounted;
- no `.env`, home directory, keyring, SSH, browser profile, or Docker socket;
- no GitHub write credential;
- only a dedicated, spend-limited model credential required by the agent CLI;
- CPU, memory, process, disk, network, output, and wall-clock limits;
- outbound network disabled or allowlisted after trusted dependency setup;
- proprietary ignored directories absent from Git and therefore absent from
  the worktree.

A prompt cannot override a missing credential or an absent mount.

### 2. Deterministic policy enforcement

Host/controller code evaluates the resulting Git objects and command exit codes:

- branch and base SHA are exactly the leased values;
- every changed path is allowlisted;
- protected files are byte-identical to the trusted base;
- file/line limits are within the task budget;
- no escaping symlink, submodule, binary, unexpected executable bit, generated
  artifact, conflict marker, or case-collision path is introduced;
- required source changes have appropriate test changes;
- no forbidden suppression, skipped test, placeholder, dangerous API, or
  secret-shaped value is present;
- trusted format, type, lint, boundary, dead-code, duplication, test, coverage,
  security, and build commands return zero;
- required acceptance checks produce machine-readable evidence.

These checks run after every implementation/remediation attempt and again in a
clean GitHub Actions runner.

### 3. Governance enforcement

GitHub controls integration even if the local controller has a bug:

- protected `main` rejects direct pushes;
- required checks cannot be skipped by the agent;
- CODEOWNERS protects workflow and quality files;
- PRs remain draft while any gate or finding is open;
- R0-R2 auto-merge requires every deterministic and independent review status;
- R3 and escalated work requires explicit user approval;
- the controller uses a fine-grained repository-scoped user-to-server token
  with only the permissions needed to dispatch Copilot tasks, manage PR
  metadata/status, and enable eligible auto-merge; the current broad personal
  `gh` token is never placed in the daemon or an agent;
- deployment/release credentials are not available to PR CI.

No agent output string can satisfy a GitHub required check by itself.

## Enforcement manifest

The controller should load a reviewed, typed manifest from trusted `main`, for
example:

```ts
interface EnforcementManifest {
  readonly version: string;
  readonly protectedPaths: readonly string[];
  readonly forbiddenPatterns: readonly ForbiddenPattern[];
  readonly requiredGates: readonly GateDefinition[];
  readonly riskPolicies: Readonly<Record<RiskClass, RiskPolicy>>;
  readonly maxParallelTasks: number;
  readonly maxRemediationRounds: number;
  readonly budgets: BudgetPolicy;
}
```

Each run records the manifest version and hash. The controller rejects a result
when the branch changes the manifest or any file used to execute gates.

Suggested protected sources:

```text
.agent-workflow/controller/**
.agent-workflow/policy/**
.agent-workflow/scripts/**
.github/workflows/**
.github/CODEOWNERS
modern-app/package.json
modern-app/package-lock.json
modern-app/tsconfig*.json
modern-app/eslint.config.*
modern-app/vitest.config.*
modern-app/playwright.config.*
modern-app/tests/invariants/**
security and quality rule configuration
```

A dedicated R3 task with explicit user approval is required to alter these files.

## Concrete Copilot and Sandcastle pipeline

The eventual controller should follow this shape. This is illustrative
pseudocode, not code to run yet:

```ts
const task = validateApprovedTask(await queue.lease());
const policy = loadPolicyFromTrustedMain();
const baseSha = await git.resolveProtectedMain();

const issue = await github.createOrReuseIssue(task);
// Versioned adapter around GitHub's public-preview Copilot assignment API.
await github.dispatchToCopilot(issue);

const pr = await github.waitForSingleCopilotPr({ issue, baseSha });
await enforceRemoteDiffFromTrustedController({ task, policy, pr, baseSha });
await github.waitForRequiredCi(pr);

const reviews = await runReadOnlySandcastleReviews({ task, pr, baseSha });
const findings = validateAndDeduplicateFindings(reviews);
await github.publishReviewStatuses(pr, findings);

if (hasBlockingFindings(findings)) {
  await github.requestCopilotRemediation({ pr, task, findings });
  await enforceRemoteDiffFromTrustedController({ task, policy, pr, baseSha });
  await github.waitForRequiredCi(pr);
  await rerunReviews(pr);
}

if (task.riskClass === "R3" || task.isEscalated) {
  await github.requestUserApproval(pr);
} else {
  await github.enableAutoMerge(pr);
}
```

Important details:

- Copilot owns its managed branch and PR; the controller does not force-push or
  create a second PR for the same task.
- Copilot PR creation, comments, or completion claims are not acceptance.
- Sandcastle reviewers use an explicit read-only worktree at the recorded PR
  SHA and cannot push, merge, or modify the PR branch.
- Do not use Sandcastle's unattended `head`, `merge-to-head`, or `noSandbox()`
  modes for local review/fallback agents.
- Do not copy `.env`, host `node_modules`, or authentication directories into a
  local worker merely for convenience.
- The local reviewer CLI needs its model endpoint, so route it through an egress
  proxy permitting only that API; a Docker network alone is not an allowlist.
- The hardened provider may need to wrap/extend Sandcastle's provider to enforce
  every resource, mount, and egress requirement.
- Auto-merge is enabled only after required statuses reference the current PR
  head SHA; a new Copilot commit invalidates prior evidence.

## Making policy context deterministic

Agents should still receive concise standards because it improves first-pass
quality, but loading them must not be left to agent choice.

The trusted implement prompt should be assembled by the controller from:

1. validated task fields;
2. a short non-negotiable rule set generated from the enforcement manifest;
3. relevant architecture decisions selected by task risk/path;
4. acceptance criteria and required tests;
5. current branch/base identifiers.

The controller records the exact resolved prompt and policy hash. Untrusted task
or issue prose is inserted as inert data, never as prompt-file shell expansion.

This proves what context was supplied, but it remains a productivity measure,
not the acceptance boundary. A fully informed agent can still make a mistake;
the external gates must catch it.

## Trusted gate execution

The agent must not choose which commands count as verification.

- Gate definitions come from the trusted manifest/controller, not from agent
  output.
- The controller invokes them through `sandbox.exec()` and checks exact exit
  codes, timeouts, and output limits.
- Protected package/tool configuration is compared with the base **before** any
  branch-controlled build script is executed.
- Dependency/lockfile changes cause an immediate stop unless the task is an
  approved R3 dependency task.
- CI starts from a clean clone and lockfile install, not the agent's existing
  `node_modules` or test cache.
- Required statuses have stable names and are configured in branch protection.

Example gate classes:

```text
policy/diff
policy/protected-files
quality/format
quality/types
quality/lint
quality/boundaries
quality/dead-code
quality/duplication
quality/unit-property
quality/coverage
security/source
security/dependencies
quality/build
quality/e2e
review/code
review/security
review/qa
```

A status exists only when its responsible program produced evidence. An agent
saying “lint passed” in prose never creates `quality/lint`.

## Preventing agents from weakening tests

Feature agents need to add tests, so test directories cannot all be read-only.
Use several controls together:

- keep critical invariant/golden tests in a protected directory;
- reject deletion, skipping, assertion removal, and threshold reduction;
- run all base tests as well as new tests;
- require changed production behaviour to map to a new/changed test in the task
  evidence;
- run coverage-diff and property-test gates;
- run mutation testing on critical domain modules on schedule and for R2
  changes when practical;
- have the QA reviewer map each acceptance criterion to test/runtime evidence;
- require separate code, security, and QA review statuses for domain money,
  clock, persistence, undo, and seating changes.

A test written by the implementer is useful evidence, but not independent proof.

## Enforcing review rather than trusting it

Reviewer prompts are also not sufficient. Review runs should:

- use a different session from implementation;
- be read-only;
- receive the task contract, base diff, and relevant policy—not the
  implementer's self-assessment;
- emit JSON matching the reviewed finding schema;
- fail if output is absent, malformed, lacks evidence, or references a path not
  in the diff without explanation;
- run separate code-quality, security, and QA roles for R2/R3 work;
- use a second model/provider for critical work when budget permits;
- block critical/high findings automatically;
- send accepted findings to a separate remediation run;
- rerun deterministic gates and reviews after repair;
- quarantine after two non-converging repair cycles.

For R0-R2, required independent review statuses and deterministic CI form the
routine acceptance boundary. R3, ambiguous, non-converging, or incident work is
escalated to the user.

## Adversarial acceptance tests

Before unattended mutation, test the controller as if the worker were trying to
bypass it. A fake/mocked agent should attempt to:

- emit `COMPLETE` without commits;
- commit outside its path allowlist;
- edit the verify script to return success;
- change `package.json` so the test command does nothing;
- delete or skip failing tests;
- add an ESLint/type/coverage suppression;
- introduce an escaping symlink or binary;
- print a secret-shaped value;
- push directly or call the GitHub API;
- alter the branch after its lease becomes stale;
- claim reviews passed without valid structured review artifacts;
- leave high-severity findings unresolved;
- loop, hang, flood output, or exceed budget;
- place shell syntax in issue/task content.

The expected result is rejection or quarantine, never a ready PR. These are
controller tests and CI fixtures, not instructions we hope agents remember.

## Residual risk

No automated process can prove that code is elegant, bug-free, or secure. A
subtle semantic defect can pass types, lint, tests, scans, and model review.
This design reduces dependence on luck by combining capability limits,
deterministic checks, independent reviews, protected integration, bounded
repair, and targeted user escalation. Critical domain invariants and small task
sizes make the remaining risk inspectable.
