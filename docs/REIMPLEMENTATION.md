# Clean-room reimplementation plan

The safest route is to recreate the product's observable behaviour and data interoperability, not copy its proprietary implementation or defeat its licensing.

## Recommended product slices

1. **Core tournament engine**
   - Pure deterministic model for clock, countdown, levels, players, transactions, rankings, pot, and prizes.
   - Commands produce domain events; time is injected for reproducible tests.

2. **Persistence and import**
   - Define a versioned JSON schema for new data.
   - Build a tightly sandboxed, one-way importer for legacy `.tdt` and template formats. Never use `eval` in the new application.

3. **Operator application**
   - Tournament setup, live controls, player transactions, seating, prizes, and history.
   - Keyboard-first workflow and undo/redo for high-pressure operation.

4. **Player display**
   - Separate read-only display client driven by state snapshots/events.
   - Layout themes built from safe components and CSS variables rather than executable HTML.

5. **Seating engine**
   - Reproduce documented constraints: table capacity, disparity, locks, unavailable seats, collapse order, dealer-relative movement, and final-table policy.
   - Make randomness seedable and auditable.

6. **Reporting and integrations**
   - HTML/CSV/JSON exports, receipts, backups, and an authenticated status API.
   - Treat third-party integrations as adapters with HTTPS-only transport.

## Suggested modern architecture

- Domain package with no UI or filesystem dependencies.
- SQLite or PostgreSQL for players/leagues/seasons; JSON documents for portable tournament snapshots.
- Typed schemas and migrations for every persisted version.
- Backend-controlled clock and event log; clients render derived state.
- Sandboxed desktop shell or browser application with no Node access in the renderer.
- Preload/context bridge with a small allowlisted IPC API if Electron is retained.
- Signed releases, current runtime, CSP, dependency scanning, and automatic backups.

## Compatibility priorities

Implement in this order:

1. Read-only legacy tournament inspection.
2. Round structures and clock semantics.
3. Player transactions and ranking order.
4. Prize/pot calculations and rounding.
5. Seating and movement suggestions.
6. Layout-token equivalence for the commonly used tokens.
7. Statistics, reports, and less-common partner formats.

## Test strategy

- Golden tests using the vendor's bundled sample `.tdt` and templates.
- Property tests for money conservation, chip totals, ranks, and seat uniqueness.
- Fake-clock tests for pause/resume, delayed timer callbacks, level rollover, and countdown modes.
- Seeded seating tests across heterogeneous table sizes and collapse orders.
- Security tests proving imported legacy text cannot execute code or escape its parser.
- End-to-end tournament fixtures from registration through winner/chop and final exports.

## Immediate next engineering task

Write a non-executing parser for the limited JavaScript-like legacy serialization. It should accept only literals, arrays, object properties, and an allowlist of constructors, then emit neutral JSON. That unlocks safe fixtures and lets the replacement engine be compared against real saved tournaments.
