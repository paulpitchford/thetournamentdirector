"""Atomic host-controlled global pause switch."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class PauseStatus:
    """Current global pause state."""

    paused: bool
    reason: str | None


class PauseSwitch:
    """Filesystem pause switch outside agent worktrees."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def status(self) -> PauseStatus:
        """Read the current pause state."""
        if not self.path.exists():
            return PauseStatus(paused=False, reason=None)
        return PauseStatus(
            paused=True,
            reason=self.path.read_text(encoding="utf-8").strip() or "unspecified",
        )

    def pause(self, reason: str) -> None:
        """Atomically enable the pause switch with an operator reason."""
        clean_reason = reason.strip()
        if not clean_reason:
            raise ValueError("pause reason must not be empty")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".tmp-{os.getpid()}")
        timestamp = datetime.now(UTC).isoformat()
        temporary.write_text(f"{timestamp} {clean_reason}\n", encoding="utf-8")
        temporary.replace(self.path)

    def resume(self) -> None:
        """Disable the global pause switch."""
        self.path.unlink(missing_ok=True)
