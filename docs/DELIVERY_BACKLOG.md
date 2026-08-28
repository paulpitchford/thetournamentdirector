# Agent-ready delivery backlog

Status: planning baseline. Tasks become dispatchable only after they are copied
into the chosen tracker, validated against the task contract in
`AGENT_ORCHESTRATION_PLAN.md`, and explicitly approved.

This backlog decomposes the version 1 product into small integration units. It
is deliberately more detailed than the build-order summary in
`MODERN_APP_PLAN.md` and includes advanced seating. Every task is governed by
`ENGINEERING_QUALITY_POLICY.md`.

## Global definition of done

Every task must:

- stay within its approved paths and non-goals;
- add or update tests for changed behaviour;
- pass the repository's canonical format, lint, typecheck, unit-test, and build
  commands;
- preserve integer minor units for cash and injected time/randomness in domain
  code;
- contain no network, browser, React, IndexedDB, or filesystem dependency in the
  domain engine;
- update versioned schemas when persisted state changes;
- produce a concise decision/evidence summary in its PR;
- avoid vendor-owned files and recovered source entirely.

A task is incomplete when it merely adds types or UI without proving its stated
acceptance criteria.

## Dependency overview

```text
HUM-001 -> FND-001 -> FND-002 -> DOM-001
                                  |-- CLK-001 -- CLK-002
                                  |-- PLY-001 -- FIN-001 -- PAY-001
                                  |                 |
                                  |-- SEAT-001 -- SEAT-002 -- SEAT-004 -- SEAT-005
                                  |       |          |                    |
                                  |       +-- SEAT-003+-- SEAT-006 -------+
                                  |                              |
                                  +-- ACT-001 <------------------+
                                         |
                                  SER-001 + PERSIST-001
                                         |
              SETUP-UI -> RUN-UI -> PLAYERS-UI -> SEATING-UI -> RESULTS-UI
                                         |
                         DISPLAY-001 -> PWA-001 -> EXPORT-001 -> E2E-001
```

The diagram is a summary; each task's `Depends on` field is authoritative.

## Wave 0 — human-owned prerequisites

### HUM-001 — Establish the trusted repository boundary

- **Status:** Complete — public remote, clean baseline, protection, and baseline
  CI verified on 2026-08-28.
- **Risk:** R3
- **Depends on:** none
- **Owner:** human
- **Objective:** initialize Git, create a protected public remote, and prove
  proprietary artifacts cannot enter ordinary worktrees.
- **Required work:** confirm `.gitignore`; inspect the initial staged file list;
  create `main`; push to the approved public remote; configure branch protection,
  CODEOWNERS, required CI, secret scanning where available, and guarded
  auto-merge that excludes R3/escalated work.
- **Acceptance:** `git ls-files` contains none of `downloads/`, `extracted/`, or
  ignored `analysis/`; a clean clone contains all authored research docs and no
  vendor binary/source; direct push to protected `main` is rejected.
- **Non-goals:** publish any recovered source; configure autonomous agents.

### HUM-002 — Approve orchestration pilot settings

- **Status:** Complete — approved controls are recorded in reviewed config;
  rootless isolation, the repository-scoped deploy key, and the global pause
  switch were proven on 2026-08-28.
- **Risk:** R3
- **Depends on:** `HUM-001`
- **Owner:** human
- **Objective:** choose provider/model, credential method, daily budget, runner,
  retention, and kill switch.
- **Acceptance:** decisions are recorded in reviewed configuration; the worker
  receives no personal GitHub token; the controller can be globally paused
  without killing the host.

## Wave 1 — project and quality foundation

### FND-001 — Scaffold the modern application

- **Risk:** R1
- **Depends on:** `HUM-001`
- **Allowed paths:** `modern-app/**`, root README links if required
- **Objective:** create the minimal React, TypeScript, and Vite application with
  a test runner, no product UI beyond a boot screen.
- **Acceptance:** clean lockfile install succeeds; development server starts;
  one unit test passes; production build succeeds; source uses strict
  TypeScript; no server, account, Electron wrapper, or vendor code is added.

### FND-002 — Canonical local and CI verification

- **Risk:** R3
- **Depends on:** `FND-001`
- **Allowed paths:** `modern-app/package.json`, lockfile, tool configuration,
  `.github/workflows/ci.yml`, `.agent-workflow/scripts/verify.sh`
- **Objective:** implement the compiler, lint, architecture, dead-code,
  duplication, test, security, and build gates required by
  `ENGINEERING_QUALITY_POLICY.md` behind one verification entry point locally
  and in GitHub Actions.
- **Acceptance:** CI runs with minimal permissions and no model/deployment
  secrets; the policy's deliberately bad fixture branches are rejected by the
  intended gates; quality configuration is protected; Actions are pinned to
  reviewed SHAs before unattended use.

### DOM-001 — Versioned domain model and command envelope

- **Risk:** R2
- **Depends on:** `FND-002`
- **Allowed paths:** `modern-app/src/domain/**`, matching tests
- **Objective:** define schema version, IDs, tournament/level/player/transaction/
  payout/clock/table/seat/movement types, command metadata, validation errors,
  and initial-state factory.
- **Acceptance:** invalid IDs, duplicate level orders, negative money/chips,
  impossible seat capacities, and unsupported schema versions are rejected;
  initial state is deterministic when IDs/time are injected; no React/browser
  import exists below `src/domain`.
- **Non-goals:** implement command behaviour or persistence.

## Wave 2 — core domain behaviour

### CLK-001 — Timestamp-derived clock controls

- **Risk:** R2
- **Depends on:** `DOM-001`
- **Allowed paths:** clock domain module and tests
- **Objective:** implement start, pause, resume, set remaining time, and add or
  subtract time using injected epoch time.
- **Acceptance:** pause/resume across delayed callbacks and background gaps has
  no drift; remaining time never derives from interval tick count; invalid time
  edits fail without state mutation; tests use no real sleeps.

### CLK-002 — Exactly-once levels and breaks

- **Risk:** R2
- **Depends on:** `CLK-001`
- **Allowed paths:** clock/level domain modules and tests
- **Objective:** implement previous/next and automatic level transitions,
  including break behaviour and end-of-structure state.
- **Acceptance:** concurrent/repeated zero observations advance one level only
  through a level ID/revision guard; breaks expose no blinds and transition
  correctly; previous/next and undo preserve a valid clock; final level does
  not loop.

### PLY-001 — Player lifecycle and entry actions

- **Risk:** R2
- **Depends on:** `DOM-001`
- **Allowed paths:** player/transaction command modules and tests
- **Objective:** implement add/rename/remove-before-start, buy-in, rebuy, add-on,
  bust, re-entry, refund, and corrected finishing position commands.
- **Acceptance:** buy-in and re-entry increment entries; rebuy/add-on do not;
  bust records time/level and provisional finish; re-entry restores active
  status; action preconditions reject illegal state transitions atomically;
  refund references and reverses an eligible transaction rather than deleting
  history.

### FIN-001 — Money, chip, and count reconciliation

- **Risk:** R2
- **Depends on:** `PLY-001`
- **Allowed paths:** finance/derived-state domain modules and tests
- **Objective:** derive price, prize contribution, fees, house-added pool, chips
  introduced, active/unique/entry counts, and average stack.
- **Acceptance:** configured paid action price equals contribution plus fee;
  all cash uses integers; pool is contributions plus house-added money; fees
  remain separate; chips stay in the tournament after a bust; division-by-zero
  states are explicit; property tests reconcile random valid command sequences.

### PAY-001 — Payout plans, assignments, and manual chop

- **Risk:** R2
- **Depends on:** `FIN-001`
- **Allowed paths:** payout domain modules and tests
- **Objective:** support percentage plans, fixed amounts, finisher assignment,
  payment markers, and a simple manually entered chop.
- **Acceptance:** allocation over pool is rejected; unallocated amount is
  derived; percentage rounding has a deterministic remainder rule; one player
  cannot occupy multiple places; chop totals equal the amount assigned to the
  chop; edits remain auditable.

## Wave 3 — advanced seating engine

### SEAT-001 — Tables, seats, assignments, and invariants

- **Risk:** R2
- **Depends on:** `DOM-001`, `PLY-001`
- **Allowed paths:** `modern-app/src/domain/seating/**`, matching tests
- **Objective:** implement table creation/order/capacity, available and
  unavailable seats, assignment/unassignment, and seat/player locks.
- **Acceptance:** an active player occupies at most one seat; a seat holds at
  most one player; unavailable seats cannot be occupied; capacity changes
  cannot orphan occupants; player and seat locks block prohibited moves; bust
  and re-entry have explicit tested seating effects.

### SEAT-002 — Deterministic random seating

- **Risk:** R2
- **Depends on:** `SEAT-001`
- **Allowed paths:** seating randomisation module and tests
- **Objective:** randomly assign eligible players to valid seats through an
  injected seedable random source.
- **Acceptance:** identical state and seed produce identical assignments;
  different test seeds exercise different valid assignments; no capacity,
  availability, uniqueness, or lock invariant is violated; insufficient seats
  fail atomically.

### SEAT-003 — Manual movement and movement history

- **Risk:** R2
- **Depends on:** `SEAT-001`
- **Allowed paths:** seating movement/history modules and tests
- **Objective:** implement manual move, swap where explicitly requested,
  suggestion acceptance/rejection, and auditable reversible movement records.
- **Acceptance:** history records from/to seats, reason, actor, and time;
  rejected suggestions do not mutate seats; undo restores exact occupancy and
  locks or fails on a documented intervening conflict; no movement record is
  silently deleted.

### SEAT-004 — Balancing suggestions

- **Risk:** R2
- **Depends on:** `SEAT-002`, `SEAT-003`
- **Allowed paths:** seating balancing module and tests
- **Objective:** suggest moves that bring open tables within configured player
  disparity while respecting capacities, locks, and unavailable seats.
- **Acceptance:** suggestions are deterministic for a seed; already balanced
  state yields no move; every accepted move improves or completes the stated
  balance objective; property tests preserve all invariants; impossible states
  explain why no valid suggestion exists.

### SEAT-005 — Consolidation and collapse order

- **Risk:** R2
- **Depends on:** `SEAT-004`
- **Allowed paths:** seating consolidation module and tests
- **Objective:** close tables in configured collapse order when remaining open
  capacity can hold their players.
- **Acceptance:** a table closes only after all movable occupants have valid
  destinations; locked/impossible occupants block closure atomically;
  unavailable seats do not count as capacity; completed consolidation records
  each move and closure; undo restores the prior valid state.

### SEAT-006 — Dealer-aware moves and final table

- **Risk:** R2
- **Depends on:** `SEAT-003`, `SEAT-004`
- **Allowed paths:** dealer/final-table seating modules and tests
- **Objective:** track dealer seats, score equivalent relative positions for
  suggested moves, and create the configured final table with optional seeded
  randomisation.
- **Acceptance:** dealer seats are valid usable seat numbers; move scoring is
  deterministic and documented; final-table transition respects locks and
  capacity or fails atomically; randomised final table is seed-repeatable;
  movement history remains complete.

## Wave 4 — activity, undo, and persistence

### ACT-001 — Command activity and bounded undo

- **Risk:** R2
- **Depends on:** `CLK-002`, `FIN-001`, `PAY-001`, `SEAT-005`, `SEAT-006`
- **Allowed paths:** command reducer/activity modules and tests
- **Objective:** route validated commands through one reducer and record a small
  reversible activity log with inverse/preimage evidence.
- **Acceptance:** failed commands produce no partial state/activity; latest
  eligible player, money, clock, payout, and seating actions can be undone;
  undo itself is recorded; intervening conflicts fail safely; non-reversible
  actions are labelled rather than pretending to support undo.

### SER-001 — Versioned snapshot validation and recovery

- **Risk:** R2
- **Depends on:** `ACT-001`
- **Allowed paths:** domain serialization/migration modules and tests
- **Objective:** serialize neutral JSON, validate on load, and restore equivalent
  derived state.
- **Acceptance:** round-trip preserves IDs, command-derived state, clock data,
  transactions, payouts, seating, and activity; unknown versions fail clearly;
  malformed/untrusted data cannot execute code or inject HTML; migrations are
  pure and fixture-tested.

### PERSIST-001 — IndexedDB autosave and recovery adapter

- **Risk:** R2
- **Depends on:** `SER-001`
- **Allowed paths:** `modern-app/src/persistence/**`, adapter tests
- **Objective:** persist versioned snapshots and activity after meaningful
  commands while keeping the domain engine storage-independent.
- **Acceptance:** refresh restores the latest committed command; interrupted or
  failed writes retain the last valid snapshot; storage failure is surfaced
  without disabling clock controls; multiple tournaments/templates have stable
  IDs; adapter tests use a controlled IndexedDB implementation.

## Wave 5 — Director user interface

### SETUP-UI — Home and tournament setup

- **Risk:** R1
- **Depends on:** `PAY-001`, `SEAT-001`, `PERSIST-001`
- **Allowed paths:** setup/home UI and component tests
- **Objective:** create/resume/duplicate/import tournaments and edit basics,
  entry profiles, blind structure, payout plan, tables, and seats.
- **Acceptance:** a first-time user can produce a valid preset tournament
  without hidden required fields; invalid money/levels/seating are explained
  before dispatch; touch and keyboard paths work; imported JSON is validated
  and displayed as text only.

### RUN-UI — Live Director screen

- **Risk:** R2
- **Depends on:** `CLK-002`, `FIN-001`, `PERSIST-001`
- **Allowed paths:** Run UI and tests
- **Objective:** keep clock, pause, time/level controls, core totals, and fast
  player actions available on desktop and phone.
- **Acceptance:** clock and pause remain visible on phone; every action dispatches
  a typed command; destructive actions confirm or offer immediate undo;
  storage/display errors do not hide clock controls; no component owns domain
  truth.

### PLAYERS-UI — Roster, actions, transactions, and finishers

- **Risk:** R1
- **Depends on:** `RUN-UI`, `PLY-001`, `PAY-001`
- **Allowed paths:** Players UI and tests
- **Objective:** provide search, status, transaction history, buy-in/rebuy/
  add-on/bust/re-entry/refund actions, and corrected finishing positions.
- **Acceptance:** active, unique, and entry counts are visibly distinct;
  illegal actions are unavailable with an explanation; bust undo is immediate;
  transaction cash/chips components are inspectable; keyboard and touch flows
  cover at least 100-player rosters.

### SEATING-UI — Advanced seating operations

- **Risk:** R2
- **Depends on:** `SEAT-005`, `SEAT-006`, `PLAYERS-UI`
- **Allowed paths:** Seating UI and tests
- **Objective:** show tables/seats, random assignment, manual moves, locks,
  balance/consolidation suggestions, dealer buttons, final-table action, and
  movement history.
- **Acceptance:** suggestions are previewed before acceptance; affected players
  and reasons are clear; locks/unavailable seats are visually distinct;
  movement undo is available where valid; no drag-only operation; charts remain
  usable at phone, tablet, and desktop sizes.

### RESULTS-UI — Pool, payouts, chop, and standings

- **Risk:** R2
- **Depends on:** `PLAYERS-UI`, `PAY-001`
- **Allowed paths:** Results UI and tests
- **Objective:** display reconciled pool/fees, allocation, payouts, manual chop,
  standings, and activity.
- **Acceptance:** over-allocation cannot be saved; unallocated money is visible;
  payout assignments and payment markers are explicit; activity links to
  affected player/transaction/movement; all money uses tournament currency.

## Wave 6 — display, offline operation, and exports

### DISPLAY-001 — Read-only player display and same-device sync

- **Risk:** R2
- **Depends on:** `RUN-UI`, `PERSIST-001`
- **Allowed paths:** display projection/UI/synchronization modules and tests
- **Objective:** open a full-screen high-contrast Display receiving sanitized
  snapshots through `BroadcastChannel`.
- **Acceptance:** Display cannot import or dispatch commands; snapshots exclude
  private activity and player details not required for display; current/next
  blinds, time, break, players, entries, average stack, and pool update; stale
  or disconnected status is visible; two themes scale to supported viewports.

### PWA-001 — Offline install, audio, and wake lock

- **Risk:** R1
- **Depends on:** `DISPLAY-001`
- **Allowed paths:** manifest/service-worker/audio/wake-lock modules and tests
- **Objective:** cache the app shell, support installation, prime level audio
  from a gesture, and expose best-effort wake-lock state.
- **Acceptance:** app starts and resumes a saved tournament with network blocked
  after first load; update strategy cannot replace the running app mid-event;
  unavailable/released wake lock is visible; clock correctness does not depend
  on service worker, audio, or wake lock.

### EXPORT-001 — JSON backup and CSV/seating exports

- **Risk:** R2
- **Depends on:** `SER-001`, `SEATING-UI`, `RESULTS-UI`
- **Allowed paths:** export modules and tests
- **Objective:** export/import versioned JSON and export final standings,
  transactions, seating list/chart data, and movement instructions in CSV or
  printable browser views.
- **Acceptance:** spreadsheet-formula injection is neutralized in CSV; Unicode,
  delimiters, quotes, and newlines round-trip; backup import validates before
  replacing state; exports reconcile with on-screen totals and seating.

### E2E-001 — Complete tournament acceptance suite

- **Risk:** R2
- **Depends on:** all prior application tasks
- **Allowed paths:** end-to-end tests and minimal fixes explicitly approved per
  failure
- **Objective:** automate representative single- and multi-table tournaments
  from setup to result and recovery.
- **Acceptance:** suite covers clock gaps, breaks, all transaction types, bust/
  undo/re-entry, balancing, consolidation, final table, chop, refresh, offline,
  Director/Display sync, JSON recovery, and CSV export at desktop/tablet/phone
  viewports; failures retain trace/screenshot evidence; repeated runs establish
  an agreed flake baseline.

## Parallel-work policy

The following may be considered for parallel execution only after their shared
foundation is merged:

- `CLK-001`, `PLY-001`, and `SEAT-001`, provided their allowed paths do not
  overlap and `DOM-001` is stable;
- `SEAT-002` and `SEAT-003` after `SEAT-001`;
- isolated UI component work after its domain contracts are merged.

Do not parallelize:

- commands that alter the central reducer/activity schema;
- serialization with active domain-schema changes;
- balancing, consolidation, and final-table policy before their predecessor
  invariants are merged;
- CI/workflow changes with any other task;
- broad refactors with feature work.

## Planning change rule

An agent may discover that a task is too large, incorrectly ordered, or missing
an invariant. It should stop and emit a structured change proposal containing:

- the exact ambiguity or conflict;
- evidence from current code/docs;
- proposed split or dependency change;
- acceptance criteria for each replacement task;
- migration impact on already merged work.

The controller marks the task `BLOCKED_REQUIREMENTS`. It must not silently let
the agent broaden scope.
