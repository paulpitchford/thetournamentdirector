# Engineering quality policy

Status: required for all application code and agent-produced changes.
Enforcement mechanics are defined in `POLICY_ENFORCEMENT.md`.

This policy turns “write professional code” into enforceable checks. Prompts and
agent confidence are not quality controls. A change is acceptable only when
compiler, static analysis, architecture checks, tests, security scans,
independent reviews, and branch protection all agree that it may proceed.

## Quality outcome

Version 1 code should be:

- correct against explicit product and domain invariants;
- easy for another engineer to understand and change;
- appropriately reusable without speculative abstraction;
- secure against untrusted imports, browser injection, CSV injection, secret
  leakage, and privilege expansion;
- deterministic where time, money, IDs, or randomness affect behaviour;
- covered by tests that can fail for the right reason;
- free from abandoned paths, placeholders, duplicated implementations, and
  suppressed diagnostics.

No single metric proves this. The workflow uses several independent layers so a
weakness in one is likely to be caught by another.

## Enforcement chain

Every agent change passes through these stages:

1. **Task guard** — one small task, explicit acceptance criteria, path allowlist,
   change-size budget, and stable dependencies.
2. **Compiler guard** — strict TypeScript rejects ambiguous and unsafe types.
3. **Static guard** — formatting, lint, dead-code, dependency-boundary,
   duplication, and complexity checks.
4. **Test guard** — unit, property, integration, accessibility, and end-to-end
   tests selected by the changed risk area.
5. **Security guard** — secret, dependency, source, and dangerous-browser-API
   scans.
6. **Independent review** — separate code-quality, security, and QA assessments
   return structured findings with file/line evidence.
7. **Remediation guard** — accepted findings go to a separate repair run, then
   every deterministic check is repeated.
8. **PR guard** — protected `main`, required clean CI and independent review
   statuses at the current head SHA, CODEOWNERS, and policy-controlled
   auto-merge; R3/escalated work requires the user.

An implementer cannot waive, reconfigure, or approve any of these stages.

## Required TypeScript settings

Use `strict: true` and enable at least:

```json
{
  "compilerOptions": {
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "noImplicitOverride": true,
    "noFallthroughCasesInSwitch": true,
    "noImplicitReturns": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "useUnknownInCatchVariables": true,
    "verbatimModuleSyntax": true
  }
}
```

Production code must not use:

- implicit or explicit `any` without a reviewed adapter-boundary exception;
- unchecked `as` casts to make an error disappear;
- non-null assertions where validation or narrowing can prove the value;
- `@ts-ignore`, `@ts-nocheck`, or broad lint-disable comments;
- floating promises, ignored rejections, or empty `catch` blocks;
- numeric enums or stringly typed state where a discriminated union is clearer.

Use `unknown` at trust boundaries, validate it, and narrow it before domain use.
Prefer exhaustive switches with a `never` assertion for command/status unions.

## Architecture rules

### Domain purity

`modern-app/src/domain/` must not import React, browser APIs, IndexedDB,
BroadcastChannel, service workers, wall-clock globals, random globals,
filesystem APIs, or network clients.

- Inject epoch time, ID generation, and seeded randomness.
- Store cash as integer minor units. Never use floating-point cash arithmetic.
- Commands validate completely before returning a new state.
- Failed commands return typed errors and make no partial mutation.
- Derived totals come from authoritative transactions/state, not duplicated UI
  counters.
- Persisted data is versioned and validated before becoming domain state.

### Dependency direction

The allowed direction is:

```text
UI -> application adapters -> domain
persistence/sync/export adapters -> domain contracts
Display -> sanitized display projection
```

The domain never imports outward. The Display never imports the command bus.
Adapters cannot reach into UI component internals. Circular dependencies are a
hard failure.

Enforce these boundaries with `dependency-cruiser` or an equivalent checked-in
rule set; do not rely on folder naming alone.

### Modules and reuse

Professional reuse means one authoritative implementation of a stable concept,
not a generic helper for every repeated line.

- Extract shared logic when it represents the same concept and has the same
  reasons to change.
- Keep domain policies named and explicit: money reconciliation, clock
  projection, seat validation, balancing score, and payout allocation should
  not be anonymous UI calculations.
- Prefer small cohesive modules and composition over inheritance.
- Avoid “utils” dumping grounds, god reducers, global service locators, and
  catch-all context objects.
- Do not create a framework, plugin system, repository abstraction, or generic
  event bus without a version 1 requirement and tests.
- A third similar implementation should trigger a reuse review. Two superficially
  similar implementations may remain separate when their domain rules differ.
- Public module APIs should expose the minimum needed surface. Unused exports
  fail dead-code checks.

### Functions and state

- Functions should do one named job and make side effects visible.
- Prefer guard clauses over deeply nested control flow.
- Avoid boolean-flag APIs; use named options or distinct commands.
- Do not mutate command inputs or prior immutable state.
- Do not mirror domain state in React local state.
- Avoid effects for values that can be derived during rendering.
- Async operations require explicit loading, success, empty, and failure states.
- Error messages shown to the Director must say what failed and whether the
  tournament remains safely usable.

Hard static-analysis limits should start with:

- cognitive complexity no greater than 15 per function;
- nesting depth no greater than 4;
- no more than 5 parameters without a named parameter object;
- no newly duplicated block of 20 or more meaningful lines;
- no production source file over 500 lines without a reviewed exception.

These are review triggers, not incentives to split code into meaningless tiny
wrappers. Any exception must explain why the larger unit is more cohesive.

## Security rules

### Untrusted data

Treat backup JSON, CSV content, tournament/player names, images, issue text, PR
comments, BroadcastChannel messages, and IndexedDB records as untrusted.

- Validate data against a versioned runtime schema.
- Never use `eval`, `Function`, executable templates, or code-bearing imports.
- Never inject imported HTML or use `dangerouslySetInnerHTML` for user content.
- Render untrusted values as text.
- Neutralize spreadsheet formula prefixes in CSV cells.
- Put explicit size/depth/count limits on imports before parsing or storing.
- Reject unknown schema versions; do not guess.

### Browser and PWA boundaries

- The Display receives a minimal sanitized projection and cannot dispatch
  commands.
- Broadcast messages include schema version, tournament ID, revision, and
  sender role; malformed or stale messages are ignored.
- Service-worker caching must not replace a running tournament unexpectedly.
- Storage, sync, wake-lock, audio, and fullscreen failures must not disable the
  authoritative clock controls.
- No external script, analytics, tracking pixel, or remote font is needed in
  version 1.
- Add a restrictive Content Security Policy before release.

### Dependencies and secrets

- A dependency addition is an R3 task requiring explicit user approval, lockfile review,
  license review, maintenance check, and vulnerability scan.
- Prefer platform APIs and focused packages over broad utility/framework
  dependencies.
- No secrets belong in source, fixtures, snapshots, logs, prompts, browser
  storage, or agent transcripts.
- Agent containers receive no personal GitHub token, SSH key, Docker socket, or
  production credential.
- Security/workflow configuration is protected and cannot be weakened by a
  feature task.

## Test quality rules

Tests must demonstrate behaviour, not merely execute lines.

- Name the condition and expected outcome.
- Use Arrange/Act/Assert or an equally clear structure.
- Assert externally meaningful state/result, not private implementation steps.
- Use injected fake time and seeded randomness; never sleep in domain tests.
- Include rejection and atomicity cases, not only happy paths.
- Every bug fix starts with a test that fails before the fix.
- Avoid broad snapshots as the only assertion.
- Mock at actual system boundaries, not every collaborator.
- No `.only`, skipped tests, unconditional retries, swallowed assertions, or
  lowered thresholds in a feature PR.
- A flaky test is a defect. Quarantine requires an owner, issue, and expiry; it
  cannot silently disappear from required CI.

### Critical domain properties

Property-based tests should generate valid and invalid command sequences and
prove:

- cash price equals prize contribution plus fee where configured;
- prize pool, fees, payouts, entries, people, and chips reconcile;
- clock pause/resume and delayed observations do not drift;
- a level revision advances at most once;
- undo restores the intended pre-command state;
- each active player occupies at most one usable seat;
- each usable seat holds at most one active player;
- capacity, locks, unavailable seats, collapse order, and final-table policy are
  never violated;
- a seed reproduces random seating and tie-break decisions;
- serialize/restore preserves equivalent derived state.

### Coverage and mutation testing

Do not chase one repository-wide percentage. Use a ratchet:

- once the scaffold baseline exists, changed executable lines should have at
  least 90% line and 85% branch coverage unless the PR documents why a line is
  unreachable or tested elsewhere;
- domain engine target: at least 95% line and 90% branch coverage;
- critical clock, money, payout, undo, import, and seating invariants require
  direct tests regardless of percentage;
- nightly mutation testing should begin after the core engine stabilizes, with
  an initial break threshold of 75% and a target ratcheted toward 85% or better.

A surviving mutation in critical reconciliation or transition logic is treated
as a missing test, not ignored to preserve the score.

## Planned deterministic toolchain

Exact packages are selected in `FND-002`, but the required capabilities are:

| Capability | Planned tool |
|---|---|
| Formatting | Prettier check, never write mode in CI |
| Type safety | `tsc --noEmit` with strict settings |
| Type-aware lint | ESLint flat config, `typescript-eslint` strict type-checked rules |
| React correctness | React Hooks and JSX accessibility lint rules |
| Complexity/code smells | ESLint complexity rules and/or SonarJS |
| Module boundaries/cycles | dependency-cruiser |
| Dead files/exports/dependencies | Knip |
| Duplicate blocks | jscpd or equivalent, reviewed threshold |
| Unit/integration tests | Vitest |
| Property tests | fast-check |
| Browser journeys | Playwright |
| Accessibility | axe integrated into Playwright |
| Mutation testing | Stryker on domain modules, scheduled |
| Source security | CodeQL and a focused Semgrep ruleset |
| Dependency risk | lockfile review, GitHub dependency review, OSV/npm audit |
| Secret scanning | GitHub secret scanning and/or Gitleaks |
| Bundle regression | explicit compressed bundle-size budget |

Adding a tool is not enough. Each tool needs a checked-in command, a failing
fixture/test of its policy where practical, and a required CI status.

## Slop patterns that block a PR

The path/static/review gates must reject or raise a finding for:

- placeholder implementations, fake data on production paths, TODO/FIXME without
  an approved issue, or commented-out code;
- tests deleted or weakened to make a change pass;
- lint/type/coverage/security suppressions added without a specific approved
  exception;
- duplicate domain calculations in components;
- `console.log`, alert-driven error handling, empty catches, or generic “failed”
  messages in production paths;
- unsafe casts, unchecked imported data, magic cash conversion, real-time sleeps,
  or unseeded domain randomness;
- huge mixed-purpose components, reducers, or “manager/service/helper” classes;
- wrappers that add no policy, validation, or useful abstraction;
- needless interfaces with one implementation and no boundary purpose;
- speculative extension points, factories, dependency injection containers, or
  plugin systems;
- copied blocks and near-identical mobile/desktop business logic;
- broad changes unrelated to the task;
- dependency additions for trivial functionality;
- generated prose comments that restate the code instead of explaining a
  decision or invariant;
- accessibility regressions, mouse-only operations, or buttons without
  accessible names;
- direct edits to generated files without changing their source.

## Independent review protocol

Review is split so “review” cannot mean the implementer looking at its own diff
again.

### Code-quality reviewer

Returns schema-validated findings for:

- correctness and edge cases;
- architecture/dependency violations;
- naming, cohesion, complexity, duplication, and unnecessary abstraction;
- unsafe types and hidden side effects;
- test strength and maintainability;
- compatibility with the task's acceptance criteria and non-goals.

### Security reviewer

Required for R2/R3 and any import, export, persistence, service-worker,
BroadcastChannel, dependency, or workflow change. Reviews trust boundaries,
injection, data validation, credentials, privacy, denial-of-service limits,
supply-chain risk, and unsafe GitHub Actions patterns.

### QA reviewer

Maps every acceptance criterion to test or runtime evidence. It checks realistic
operator journeys, mobile/keyboard accessibility, failure recovery, offline
behaviour, and regressions outside the changed module.

### Finding schema

Every structured Codex finding contains:

```text
id
reviewType
severity: critical | high | medium | low | note
path
line or symbol
evidence
risk
requiredAction
suggestedTest
confidence
```

Critical/high findings always block. Medium findings block unless the user
records a time-limited exception. Low/note findings cannot trigger unbounded
polishing. Current Copilot PR-review comments are blocking by default because
Copilot returns comment-only reviews rather than this structured severity
schema; the controller routes them through bounded remediation/re-review.

## Remediation and rejection

When a guard detects poor code:

1. Controller keeps the PR in draft and records the failing command/finding.
2. A remediation agent receives the original task plus accepted findings.
3. It may edit only the original allowed paths and relevant tests.
4. Compiler, lint, architecture, tests, security scans, and all reviews rerun
   from the beginning.
5. At most two remediation rounds are allowed.
6. Non-convergence, repeated scope growth, or a second high-severity security
   finding quarantines the task for human diagnosis.

The workflow never asks the original implementer, “Is this clean now?” and
accepts yes as evidence.

## Protected quality configuration

These files are R3/protected once created:

```text
.github/workflows/**
.github/CODEOWNERS
.agent-workflow/**
modern-app/tsconfig*.json
modern-app/eslint.config.*
modern-app/vitest.config.*
modern-app/playwright.config.*
modern-app/package.json
modern-app/package-lock.json
quality/security rule configuration
```

A normal feature task cannot change them. A dedicated quality-policy PR must:

- explain the reason;
- show before/after gate output;
- prove the change does not simply hide an existing failure;
- receive human CODEOWNER approval.

## Exceptions

A quality exception is structured data, not an inline excuse. It needs:

- rule/check identifier;
- exact path and smallest possible scope;
- technical reason alternatives are worse;
- owner;
- approval reference;
- expiry date or removal task.

Expired exceptions fail CI. Blanket and permanent exceptions are not allowed.

## Quality evidence on every PR

The controller maintains a PR section containing:

- task ID, risk class, allowed paths, and base/head SHA;
- changed file/line counts;
- commands run and exit status;
- unit/property/E2E tests added or changed;
- coverage delta and mutation result when applicable;
- dependency, secret, and source-scan result;
- code/security/QA review finding counts and remediation commits;
- remaining exceptions and required user escalations.

The user or a later auditor should be able to determine why the change was
considered safe without reading agent transcripts.

## Initial quality acceptance test

Before code agents are allowed to work unattended, deliberately submit fixture
branches containing:

1. `any`, `@ts-ignore`, an unsafe cast, and a floating promise;
2. a domain import of React or `Date.now()`;
3. a circular dependency and dead export;
4. duplicated transaction calculation;
5. a removed assertion and skipped test;
6. `dangerouslySetInnerHTML`, `eval`, and CSV formula injection;
7. a secret-shaped value and unapproved dependency;
8. a protected-path change;
9. a high-complexity function and oversized source file;
10. a reviewer high-severity finding that the first repair does not fix.

Each fixture must be rejected by the expected deterministic or review gate. The
last fixture must demonstrate bounded remediation and quarantine. The pilot is
not ready if the system merely logs these failures and continues toward merge.
