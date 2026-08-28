# Modern tournament clock — product and build plan

## Recommendation

Build a **local-first progressive web app (PWA)** with two views:

- **Director** — setup, players, money, clock controls, and results.
- **Display** — a read-only, full-screen clock for players or a projector.

It will use one responsive codebase and work in current browsers on desktop,
tablet, and phone. It can be installed where the browser supports PWA
installation and will continue to run without internet after its first load.
Start without accounts, subscriptions, a cloud database, or native wrappers.

The product promise should be: **create a tournament in two minutes, then run it
from one uncluttered screen.**

## Core version 1

### Tournament setup

- Name, date, currency, and optional venue.
- Buy-in price, prize contribution, fee, starting chips, and house-added money.
- Optional re-entry/rebuy and add-on definitions with price and chip amount.
- A few editable blind presets plus a blank structure.
- Payouts as percentages, fixed amounts, or a manual split.

### Clock and blind structure

- Timed blind levels and breaks.
- Small blind, big blind, and optional ante per level.
- Start, pause, resume, previous/next level, and add/subtract one minute.
- Edit the current time safely while paused or running.
- Automatic level advance, configurable warning, and end-of-level sounds.
- Clear display of current level, next level, elapsed level, and next break.

### Players and entries

- Add, rename, search, and remove players before play starts.
- Record buy-in, re-entry, rebuy, add-on, refund, and bust.
- Show active players, unique players, total entries, and finishing position.
- Record a bust's time and blind level, with an immediate `Undo bust` action.
- One-step undo for the latest player or money action.
- Fast mobile actions: `Buy in`, `Rebuy`, `Add-on`, and `Bust`.

The actions have distinct meanings:

- **Buy-in** — the player's first entry; adds starting chips and one entry.
- **Rebuy** — adds chips while the player remains active; does not add an entry.
- **Add-on** — adds the configured chips, normally at a defined level or break;
  does not add an entry.
- **Bust** — marks the player out, records the level/time, and assigns a
  provisional finishing place that the Director can correct.
- **Re-entry** — returns a busted player with a new stack and adds one entry.

Each paid action records price, prize contribution, fee, and chips separately.

### Advanced seating

- Configure multiple tables with individual capacities and unavailable seats.
- Randomly assign active players to valid seats using seedable randomness.
- Move players manually and lock players or seats when they must not move.
- Suggest and apply automatic table balancing while respecting capacity,
  locks, unavailable seats, and configurable maximum table disparity.
- Consolidate and close tables according to an explicit collapse order.
- Track dealer buttons and prefer dealer-relative equivalent positions when
  suggesting moves.
- Support configurable final-table creation and optional final-table
  randomisation.
- Keep every seating suggestion and accepted move in an auditable, reversible
  movement history.
- Export and print the seating chart, seating list, and movement instructions.

### Money and prizes

- Calculate the prize pool from each transaction's prize contribution, not just
  its selling price.
- Show fees and house-added money separately.
- Validate that allocated payouts do not exceed the pool and highlight any
  unallocated amount.
- Assign finishers to places and support a simple manually entered chop.
- Use integer minor units for money; never use floating-point values for cash.

### Player display

- Tournament name and optional venue/logo.
- Large remaining time, current blinds/ante, and next blinds.
- Players remaining, entries, average stack, and prize pool.
- Break messaging and the next scheduled break.
- Full-screen mode, two high-contrast themes, responsive scaling, and a
  keep-screen-awake control.

### Continuity and exports

- Autosave every meaningful command and resume the last tournament after a
  refresh or restart.
- Duplicate a tournament as a reusable template.
- Import/export a versioned JSON backup.
- Export final standings and transactions as CSV.
- Keep a small activity log so mistakes can be inspected and undone.

## Optional version 1.1

Add a read-only remote Display on another physical device only if early users
need it. Use an optional relay with an expiring room code or QR code; the local
Director remains authoritative and keeps working if the relay disconnects.

## Explicitly outside version 1

- Accounts, permissions, subscriptions, and cloud sync.
- Leagues, seasons, lifetime statistics, and points formulas.
- ICM calculations, bounty accounting, satellite seat awards, and chip races.
- Arbitrary formulas, scripts, event automation, or a visual layout designer.
- Receipt printers, partner formats, live streaming integrations, and public
  APIs.
- Multiple simultaneous director devices.
- App-store packaging or an Electron desktop edition.

These can be added only in response to demonstrated use, rather than carried
over because the legacy product has them.

## User experience

Use seven small areas rather than a large settings window:

1. **Home** — new from preset, resume, duplicate, import, or delete.
2. **Setup** — basics, entry options, blind structure, and payout plan.
3. **Run** — the main operator screen used during play.
4. **Players** — roster, transactions, status, and finishing positions.
5. **Seating** — tables, assignments, balancing, moves, and movement history.
6. **Display** — open the player-facing view in a separate window/tab.
7. **Results** — payouts, final standings, activity, and export.

The Run screen should keep only high-frequency information visible:

```text
+------------------------------------------------------------+
| Level 5     08:42     Blinds 400 / 800 (800 ante)          |
| [Pause] [−1 min] [+1 min] [Previous] [Next level]          |
+-------------------------------+----------------------------+
| 24 active / 31 entries        | Pool £1,550 / Fees £155   |
| Next: 500 / 1,000 (1,000)     | Next break in 23:42       |
+-------------------------------+----------------------------+
| [Buy in] [Rebuy] [Add-on] [Bust] [Open display]            |
+------------------------------------------------------------+
```

On a phone, the clock and pause button remain sticky while the statistics and
player actions collapse into cards/drawers. Destructive actions require a clear
confirmation or offer immediate undo.

## Lightweight architecture

Recommended implementation: **React, TypeScript, Vite, and standard browser
APIs**. Keep the tournament engine in plain TypeScript with no React dependency.
A reducer accepts typed commands and returns the next state plus an activity
record.

```text
Director UI -> typed command -> tournament engine -> current snapshot
                                      |                    |
                                      v                    v
                                  activity log         IndexedDB
                                                           |
                                                           v
                                                BroadcastChannel -> Display
```

- **IndexedDB** stores tournament snapshots, templates, and activity records.
- A **service worker** caches the application shell for offline use.
- **BroadcastChannel** sends sanitized display snapshots to another tab or
  window on the same browser/device.
- The **Screen Wake Lock API** is requested while the clock/display is active;
  the UI must show when it is unavailable or has been released.
- A web app manifest provides installation, icons, and standalone presentation.
- The display accepts snapshots only; it cannot issue tournament commands.

Do not add a server to version 1. For a display on a second physical device,
later add a small optional relay: the Director creates an expiring room code or
QR code and publishes read-only display snapshots over a WebSocket. The local
Director remains authoritative and continues working if the relay disconnects.

## Clock design

Never make a one-second interval the source of truth. Browsers throttle timers
in background tabs and devices can briefly suspend work.

Persist clock state as:

```text
levelId
status: stopped | running | paused
elapsedBeforeRunMs
resumedAtEpochMs
```

While running:

```text
remaining = level duration - elapsedBeforeRun - (now - resumedAt)
```

The visual timer may redraw several times per second, but only operator commands
and level transitions change stored state. This keeps the clock accurate after
rendering delays, refreshes, or background throttling. Guard each zero-time
transition with the current level ID/revision so it can happen only once.

Starting the clock should also prime audio through that user gesture. If the
computer sleeps or its system time changes, the app should show the detected
time jump and make the `Edit time` control immediately available.

## Minimal data model

```text
Tournament
  id, revision, name, currency, status, settings, houseAddedMinor

Level
  id, order, kind(round|break), durationSeconds, smallBlind, bigBlind, ante

Player
  id, displayName, status(registered|active|busted|withdrawn), seat?, finish?

Transaction
  id, playerId?, kind(buyin|reentry|rebuy|addon|refund),
  priceMinor, prizeContributionMinor, chips, occurredAt

Table
  id, name, order, capacity, dealerSeat?, status(open|closed)

Seat
  tableId, number, status(available|unavailable), playerId?, locked

Movement
  id, playerId, fromSeat?, toSeat?, reason, suggestedAt, acceptedAt?, undoneAt?

Payout
  place, plannedAmountMinor, playerId?, paidAt?

Clock
  levelId, status, elapsedBeforeRunMs, resumedAtEpochMs?

Activity
  id, command, occurredAt, reversible, inverseCommand?
```

All persisted documents carry a schema version and are validated on load.
Imported JSON is treated as data only; there is no executable configuration,
HTML injection, or `eval`.

## Important behaviour rules

- The Director is the only writer; the Display is always read-only.
- Every command is validated by the engine before state changes.
- A player can have multiple paid transactions but only one current status.
- Each active player has at most one seat and each usable seat holds at most one
  active player.
- Seating suggestions must respect capacity, locks, unavailable seats,
  configured table disparity, collapse order, and final-table policy.
- Random seating and movement tie-breaks are seedable; suggestions, accepted
  moves, and reversals are recorded in movement history.
- `Active players` counts people; `entries` is buy-ins plus re-entries. Rebuys
  and add-ons do not increase it.
- Average stack is total chips put into play divided by active players. Chips
  transfer on a bust; they are not removed from the tournament.
- Money totals must reconcile from transactions; display formatting uses the
  tournament currency and locale.
- Clock controls remain usable if storage or display synchronization fails.
- The app never requires internet during a live tournament.

## Build order

1. Implement and test the framework-independent tournament/clock engine.
2. Add setup presets and the single Run screen.
3. Add players, transactions, pool calculation, payouts, and undo.
4. Add the advanced seating engine, seating UI, movement history, and seating
   exports.
5. Add IndexedDB autosave, recovery, JSON backup, and CSV results.
6. Add the read-only Display, same-device synchronization, offline install, and
   wake-lock/audio handling.
7. Test complete single- and multi-table tournaments on desktop, tablet, phone,
   refresh, offline, and system sleep/wake.
8. Pilot the product before deciding whether remote display is the next module.

## Version 1 success checks

- A first-time user can start a preset tournament within two minutes.
- The live clock can be controlled without leaving the Run screen.
- Refreshing either view loses no committed action and produces no clock drift.
- Pool, fees, payout allocation, player count, and entry count always reconcile.
- Seats remain unique and valid through random assignment, balancing,
  consolidation, locks, unavailable seats, and final-table transitions.
- Every accepted seating move is auditable and reversible.
- The app works after network loss and visibly reports storage/display issues.
- Common actions work comfortably by touch and keyboard.
- A complete tournament can be restored from JSON and exported to CSV.

## Technical basis

- [MDN: Progressive web apps](https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps)
- [MDN: Service Worker API](https://developer.mozilla.org/en-US/docs/Web/API/Service_Worker_API)
- [MDN: IndexedDB API](https://developer.mozilla.org/en-US/docs/Web/API/IndexedDB_API)
- [MDN: Broadcast Channel API](https://developer.mozilla.org/en-US/docs/Web/API/Broadcast_Channel_API)
- [MDN: Screen Wake Lock API](https://developer.mozilla.org/en-US/docs/Web/API/Screen_Wake_Lock_API)
