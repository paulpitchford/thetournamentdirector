"""Command-line operator interface for the controller foundation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_config
from .controller import Controller
from .pause import PauseSwitch
from .provider import FakeProvider
from .state import RUN_ROLES, RunLedger


def repository_root() -> Path:
    """Return the repository root derived from this installed source tree."""
    return Path(__file__).resolve().parents[3]


def build_parser() -> argparse.ArgumentParser:
    """Build the controller command-line parser."""
    parser = argparse.ArgumentParser(description="Tournament Director controller")
    parser.add_argument(
        "--config",
        type=Path,
        default=repository_root() / ".agent-workflow/policy/pilot.json",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    pause = subcommands.add_parser("pause")
    pause.add_argument("--reason", required=True)
    subcommands.add_parser("resume")
    subcommands.add_parser("status")

    fake_run = subcommands.add_parser("fake-run")
    fake_run.add_argument("--task", required=True)
    fake_run.add_argument("--role", required=True, choices=sorted(RUN_ROLES))
    return parser


def main() -> int:
    """Execute one finite operator command."""
    arguments = build_parser().parse_args()
    root = repository_root()
    config = load_config(arguments.config)
    pause_switch = PauseSwitch(root / config.controller.pause_file)
    ledger = RunLedger(root / config.controller.database, config.limits)

    if arguments.command == "pause":
        pause_switch.pause(arguments.reason)
        print("Controller paused.")
        return 0
    if arguments.command == "resume":
        pause_switch.resume()
        print("Controller resumed.")
        return 0
    if arguments.command == "status":
        pause_status = pause_switch.status()
        print(
            json.dumps(
                {
                    "paused": pause_status.paused,
                    "pauseReason": pause_status.reason,
                    "runs": ledger.summary(),
                },
                sort_keys=True,
            )
        )
        return 0

    controller = Controller(
        ledger=ledger,
        pause_switch=pause_switch,
        provider=FakeProvider(),
    )
    result = controller.run(task_id=arguments.task, role=arguments.role)
    print(json.dumps(result.__dict__, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
