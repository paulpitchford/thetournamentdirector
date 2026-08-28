# Agent handoff

## Current status

Research and product planning are complete. Implementation of the replacement
app has **not** started.

Working directory:

```text
/home/paul/projects/thetournamentdirector
```

Read these first:

1. `docs/MODERN_APP_PLAN.md` — agreed product, UX, architecture, and build order.
2. `docs/AGENT_ORCHESTRATION_RESEARCH.md` — Sandcastle and alternative research.
3. `docs/AGENT_ORCHESTRATION_PLAN.md` — proposed guarded, continuous workflow.
4. `docs/ENGINEERING_QUALITY_POLICY.md` — required code, test, and review gates.
5. `docs/POLICY_ENFORCEMENT.md` — how controller code enforces those gates.
6. `docs/AGENT_CAPACITY_POLICY.md` — limits, budgets, cooldown, and recovery.
7. `docs/DELIVERY_BACKLOG.md` — dependency-ordered, agent-ready application work.
8. `docs/adr/README.md` — proposed decisions awaiting user approval.
9. `docs/PRODUCT.md` — observed behaviour of the legacy application.
10. `docs/TECHNICAL.md` — legacy implementation findings and security issues.
11. `docs/REIMPLEMENTATION.md` — wider clean-room considerations.

## User's goal

Create a deliberately simpler, modern tournament director that runs across
desktop, tablet, and phone. It should be quick to configure and operable during
a live poker tournament without the legacy application's complexity.

The agreed direction is a **local-first installable PWA** with:

- A Director view for setup and live control.
- A separate read-only, full-screen player Display.
- Offline operation after the initial load.
- No required account, server, subscription, or native wrapper in version 1.

## Required version 1 scope

- Tournament name, currency, buy-in, fees, starting stack, and added money.
- Editable blind levels and breaks with small blind, big blind, and ante.
- Resilient start, pause, resume, time adjustment, and level controls.
- Players, buy-ins, rebuys, add-ons, re-entries, refunds, and busts.
- Active-player, unique-player, and entry counts.
- Advanced seating: configurable tables and seats, random assignment, manual moves,
  automatic balancing and consolidation, seat/player locks, unavailable seats,
  dealer-button-aware movement, final-table handling, and reversible movement history.
- Prize pool, fees, payouts, finishers, and a simple manual chop.
- Full-screen player display with current/next blinds and key totals.
- Autosave/recovery, JSON backup, CSV results, and immediate undo.

### Action semantics

- **Buy-in:** first entry; adds starting chips and increments entries.
- **Rebuy:** adds configured chips without incrementing entries. If a player has
  no chips but is still eligible, record the rebuy before confirming a bust.
- **Add-on:** adds configured chips without incrementing entries, normally at a
  configured level or break.
- **Bust:** confirms elimination, records time/level, and assigns a provisional
  finishing position. It must be immediately reversible.
- **Re-entry:** returns a confirmed busted player with a new starting stack and
  increments entries.
- **Refund:** reverses the applicable price, contribution, fee, and chip effect
  through an explicit auditable transaction.

Every paid transaction stores its selling price, prize contribution, fee, and
chip amount separately. The prize pool is the sum of prize contributions plus
house-added money; fees are displayed separately. Do not use floating point for
cash.

## Important product boundary

Keep these out of version 1:

- Accounts, cloud sync, roles, subscriptions, and simultaneous Directors.
- Leagues, seasons, lifetime statistics, and formula-based points.
- ICM, bounty accounting, satellites, and chip-race tooling.
- Scriptable events, arbitrary formulas, and a visual layout designer.
- Receipt-printer, streaming, partner, or public API integrations.

Advanced seating is part of version 1. Remote display on another physical
device remains a possible version 1.1 module, not a foundation for the MVP.

## Technical decisions

- Use React, TypeScript, and Vite for the PWA.
- Keep the tournament engine in framework-independent TypeScript.
- Model changes as validated typed commands handled by a reducer.
- Save versioned snapshots and a small reversible activity log in IndexedDB.
- Cache the app shell with a service worker.
- Synchronize same-device Director/Display windows with BroadcastChannel.
- Send sanitized snapshots to the Display; it must never issue commands.
- Use Screen Wake Lock as a best-effort enhancement with a visible status.
- Treat imports as validated data only: no executable configuration or `eval`.

The clock must be timestamp-derived, not a counter decremented by `setInterval`:

```text
remaining = level duration - elapsedBeforeRun - (now - resumedAt)
```

Persist clock status, level ID, elapsed time before the current run, and the
resume epoch. Inject time into the domain engine for deterministic tests. A
level-zero transition must be guarded by level ID/revision so it occurs once.

## Agent workflow experiment

The user wants to trial an always-on orchestrator that works from a detailed
plan and manages isolated agents, builds, QA, reviews, branches, and PRs with
strong guards. The accepted design uses Copilot coding agent as the routine
implementer and PR owner, with Sandcastle for independent planning/review roles,
not as the durable authority. GitHub issues/PRs, deterministic CI, a thin local
controller, bounded jobs, and policy-controlled auto-merge form the experiment.
`docs/ENGINEERING_QUALITY_POLICY.md` defines mandatory compiler, lint,
architecture, testing, security, independent-review, and remediation gates so
agent-written code cannot progress merely because an agent claims it is done.
`docs/POLICY_ENFORCEMENT.md` makes the trust boundary explicit: Sandcastle only
runs bounded independent reviewers/fallback workers; trusted controller code
checks Copilot's PR diff and statuses, while GitHub branch protection controls
policy auto-merge and routes only R3/escalated work to the user.
`docs/AGENT_CAPACITY_POLICY.md` defines safe unattended behaviour when Codex or
another provider hits a rate, account, context, or spend limit: preserve Git
work, enter a durable cooldown, avoid blind retries, reserve review capacity,
and resume only when policy permits.

Do not start an unattended mutating loop yet. Complete Stage 0 in
`docs/AGENT_ORCHESTRATION_PLAN.md` first. This workspace is now a local Git
repository on `main` with a verified clean baseline commit, but it has no remote
or branch protection and the current user cannot access the Docker daemon. Never pass the current broad personal `gh` token into an agent
container.

## Next concrete tasks

1. Ask the user for the five remaining pilot choices listed at the end of
   `docs/AGENT_ORCHESTRATION_PLAN.md`.
2. Complete the human-owned `HUM-001` and `HUM-002` backlog tasks.
3. Run the read-only planning pilot before permitting an agent to modify code.
4. Then create the application under:

```text
/home/paul/projects/thetournamentdirector/modern-app
```

The application milestone is decomposed in `docs/DELIVERY_BACKLOG.md`. Its first
engineering sequence is scaffold and verification, versioned domain types,
clock and player/finance behaviour, advanced seating, activity/undo, and
serialization before building Setup and Run screens.

Minimum tests for that milestone:

- Pause/resume and background time gaps do not cause drift.
- A level advances once at zero and a break behaves correctly.
- Buy-ins/re-entries increment entries; rebuys/add-ons do not.
- Bust and undo restore the correct player status and finish ordering.
- Price equals prize contribution plus fee for configured paid actions.
- Pool, fees, payouts, player count, entry count, and chips reconcile.
- Seats remain unique and valid through random assignment, balancing,
  consolidation, locks, unavailable seats, and final-table transitions.
- Seating randomness is seedable and every suggested or accepted move is
  auditable and reversible.
- A serialized tournament restores to the same derived state.

## Workspace and safety

- Local Git was initialized on `main`; baseline commit `293886b` excludes all
  prohibited vendor/recovered paths. No remote is configured yet.
- `downloads/`, `extracted/`, and `analysis/decrypted/` contain proprietary
  vendor material and are ignored by the workspace `.gitignore`.
- Do not redistribute vendor binaries or recovered source.
- Keep ignored vendor artifacts out of Git and all agent worktrees.
- Do not expose host credentials, the Docker socket, or broad filesystem mounts
  to agent containers.
- Do not run the downloaded Windows installer or application unless the user
  explicitly asks and an isolated execution plan is agreed.
- Do not bypass licensing. Reimplement observable behaviour using original
  code, tests, schemas, and UI.
- Preserve the existing research files and checksums.

## Remaining product choices

The next agent can use neutral product defaults without blocking implementation.
Ask the user later for the product name/branding, preferred blind presets,
default currency, and whether remote display should follow the MVP.

The orchestration pilot still requires explicit choices before mutation:
private GitHub remote, Sandcastle review provider, credential and spend limits,
local versus dedicated review runner, and artifact/transcript retention.
Copilot coding agent PR ownership and R0-R2 policy auto-merge are accepted;
R3/escalated work requires the user.
