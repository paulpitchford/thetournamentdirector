#!/usr/bin/env python3
"""Verify the repository boundary and documentation baseline."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

PROHIBITED_PREFIXES = (
    "downloads/",
    "extracted/",
    "analysis/decrypted/",
    "analysis/electron-asar/",
    "analysis/signatures/",
)
TEXT_SUFFIXES = {".js", ".json", ".md", ".py", ".sh", ".txt", ".yaml", ".yml"}
MARKDOWN_LINK = re.compile(r"\[[^]]+\]\(([^)]+)\)")


def tracked_paths(root: Path) -> list[Path]:
    """Return paths tracked by Git relative to ``root``."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [Path(raw.decode()) for raw in result.stdout.split(b"\0") if raw]


def check_repository(root: Path) -> list[str]:
    """Return policy violations found in the tracked repository."""
    violations: list[str] = []
    paths = tracked_paths(root)
    casefolded: dict[str, Path] = {}

    for relative_path in paths:
        posix_path = relative_path.as_posix()
        if posix_path.startswith(PROHIBITED_PREFIXES):
            violations.append(f"prohibited tracked path: {posix_path}")

        folded = posix_path.casefold()
        previous = casefolded.get(folded)
        if previous is not None and previous != relative_path:
            violations.append(f"case-colliding paths: {previous} and {relative_path}")
        casefolded[folded] = relative_path

        absolute_path = root / relative_path
        if absolute_path.is_symlink():
            target = absolute_path.resolve(strict=False)
            if not target.is_relative_to(root):
                violations.append(f"escaping symlink: {posix_path} -> {target}")

        if relative_path.suffix.lower() in TEXT_SUFFIXES:
            text = absolute_path.read_text(encoding="utf-8")
            if any(line.rstrip() != line for line in text.splitlines()):
                violations.append(f"trailing whitespace: {posix_path}")

        if relative_path.suffix.lower() != ".md":
            continue

        text = absolute_path.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK.findall(text):
            target = raw_target.strip()
            if not target or target.startswith(("#", "mailto:")) or "://" in target:
                continue
            target_path = unquote(target.split("#", maxsplit=1)[0])
            if target_path and not (absolute_path.parent / target_path).resolve().exists():
                violations.append(f"broken Markdown link in {posix_path}: {target}")

    return violations


def main() -> int:
    """Print violations and return a non-zero status when checks fail."""
    root = Path(__file__).resolve().parents[2]
    violations = check_repository(root)
    if violations:
        print("Repository policy checks failed:")
        print("\n".join(f"- {violation}" for violation in violations))
        return 1
    print("Repository policy checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
