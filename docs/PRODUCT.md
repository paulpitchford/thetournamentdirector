# Product behaviour

## Purpose

The Tournament Director is a single-computer control system for a live poker tournament. It combines the tournament clock, operator workflow, public display, financial accounting, seating, results, and league statistics in one Windows application.

The vendor offers a fully featured 30-day evaluation. As checked on 2026-08-28, a personal version-3 license is USD 49.99 and a commercial license is USD 299.99 per year.

## Typical workflow

1. Create a tournament manually or with the Quick Start wizard.
2. Configure buy-ins, rebuys, add-ons, rake, bounties, chips, points, and guarantees.
3. Define timed rounds and breaks, including game changes, blinds, antes, and limits.
4. Add/import players and optionally associate them with the persistent player database, league, and season.
5. Create tables, randomise seating, lock seats, and place dealer buttons.
6. Design or select the public display layout.
7. Start the countdown/tournament and operate it from the Game window, Controls tab, or hotkeys.
8. Record buy-ins, rebuys, add-ons, eliminations, bounties, chip counts, and chops while the clock runs.
9. Accept suggested seat moves as tables become uneven or consolidate.
10. Save results, calculate prizes and rankings, print receipts/reports, and export results.

## Operator interface

The Settings window contains these tabs:

- Game, Rounds, Players, Prizes, Tables
- Layout, Events, Chips, Rules, Summary
- Database, Stats, Preferences, Hotkeys, Help, Links, Controls

The separate Game window is intended for the player-facing display. It can rotate between:

- Custom tournament screens
- Player rankings
- Seating chart
- Player movement instructions
- Blinds schedule
- Seating list

On multi-monitor systems the Game window can remain full-screen for players while the operator uses Settings elsewhere.

## Main capabilities

- Limit, pot-limit, and no-limit structures, with game changes by level
- Countdown, tournament clock, breaks, manual level changes, and hand timer
- Buy-in/rebuy/add-on profiles, rake, guaranteed pot, house contribution, bounties, receipts, and prize calculation
- Player database with contact details, leagues, seasons, images, and cross-tournament statistics
- Tracked-player mode and a simpler aggregate-count mode
- Random seating, table balancing/consolidation, locked seats, unavailable seats, final-table handling, and undo/redo
- Prize suggestions, automatic payout levels, rounding, manual recipients, chops, chip-count and ICM chop calculations
- Formula-driven points, event conditions, layout conditions, and display values
- Event automation for sound, overlay message, clock pause/resume, save, screen flash, and launching a configured program
- Highly configurable HTML/CSS display layouts with 156 shipped token types, screen sets, transitions, banners, images, and buttons
- HTML, CSV, XML, text, print, receipt, and partner-specific exports
- Scheduled/event-driven status publishing to a file or user-configured HTTP endpoint in JSON or form-variable format
- Automatic backups, autosave, language packs, and automatic application updates

## Behavioural model

The application is event-driven. Actions such as a clock tick, level change, buy-in, bust-out, table balance, or tournament end update the central `Tournament` object and publish a notification. Subscribers then refresh UI pages, run configured event actions, update the public display, autosave, or publish status.

The clock is derived from wall-clock timestamps rather than only decrementing a counter. This lets it account for pauses and for delays in the JavaScript timer loop.

Table balancing uses randomised choices constrained by available seats, table capacity, maximum player disparity, locked players, dealer-button-relative position, configured collapse order, and optional final-table randomisation.
