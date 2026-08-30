"""Apply one validated replacement in the sole attested worker container."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Protocol

from .attested_payload_runner import AttestedPayloadRunner, PayloadCommand
from .podman_mount_policy import MountPolicyFixture
from .replacement_outcome import (
    ReplacementOutcomeError,
    require_applied,
    require_definitive_rejection,
)
from .review_runtime import ProcessOutput
from .text_mutation_contract import (
    MAX_FILE_BYTES,
    TextMutationProposal,
    TextReplacement,
)
from .workspace_identity_handle import WorkspaceIdentityHandle

SAFE_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}")
REPLACEMENT_SCRIPT = r"""
relative=$1
expected=$2
replacement=$3
target=/workspace/$relative
parent=${target%/*}
test "$parent" != "$target"
test "$(readlink -f "$parent")" = "$parent"
test -d "$parent"
test ! -L "$target"
test -f "$target"
test "$(stat -c '%u' "$target")" = "$(id -u)"
test "$(stat -c '%h' "$target")" = 1
mode=$(stat -c '%a' "$target")
case "$mode" in 600|644|700|755) ;; *) exit 51 ;; esac
actual=$(sha256sum "$target")
test "${actual%% *}" = "$expected"
temporary=$(mktemp "$parent/.td-replace.XXXXXX")
trap 'rm -f "$temporary"' EXIT HUP INT TERM
cat >"$temporary"
actual=$(sha256sum "$temporary")
test "${actual%% *}" = "$replacement"
chmod "$mode" "$temporary"
sync -d "$temporary"
mv -T "$temporary" "$target" || exit 52
test "$(readlink -f "$parent")" = "$parent" || exit 52
test ! -L "$target" || exit 52
test -f "$target" || exit 52
test "$(stat -c '%h' "$target")" = 1 || exit 52
actual=$(sha256sum "$target") || exit 52
test "${actual%% *}" = "$replacement" || exit 52
sync -f "$parent" || exit 52
printf 'text-replacement-ok\n' || exit 52
trap - EXIT HUP INT TERM || exit 52
""".strip()


class ReplacementRunner(Protocol):
    def run(
        self, fixture: MountPolicyFixture, handle: WorkspaceIdentityHandle,
        payload: PayloadCommand, *, input_bytes: bytes = b"",
    ) -> ProcessOutput:
        ...


class AttestedTextReplacementError(RuntimeError):
    """Raised when replacement inputs fail before container dispatch."""


class AttestedTextReplacementApplier:
    """Dispatch one fixed replacement while the sole workspace handle is held.

    The argv runner holds the controller's only workspace handle for the entire
    container lifetime, serializing controller-authorized operations. Processes
    already running as the controller UID remain in the ADR 0009 TCB.
    """

    def __init__(self, *, runner: ReplacementRunner | None = None) -> None:
        if runner is not None and not callable(getattr(runner, "run", None)):
            raise AttestedTextReplacementError("replacement runner is invalid")
        self._runner = runner or AttestedPayloadRunner()

    def run(
        self, fixture: MountPolicyFixture, handle: WorkspaceIdentityHandle,
        proposal: TextMutationProposal,
    ) -> ProcessOutput:
        """Return bounded process evidence for the shared outcome protocol."""
        payload, content = self._prepare(handle, proposal)
        return self._runner.run(
            fixture, handle, payload, input_bytes=content
        )

    def apply(
        self, fixture: MountPolicyFixture, handle: WorkspaceIdentityHandle,
        proposal: TextMutationProposal,
    ) -> None:
        """Require exact durable applied evidence or raise the outcome error."""
        require_applied(lambda: self.run(fixture, handle, proposal))

    @staticmethod
    def _prepare(
        handle: WorkspaceIdentityHandle, proposal: TextMutationProposal,
    ) -> tuple[PayloadCommand, bytes]:
        if not isinstance(handle, WorkspaceIdentityHandle):
            raise AttestedTextReplacementError("workspace handle is invalid")
        if (
            not isinstance(proposal, TextMutationProposal)
            or proposal.task_id != handle.identity.task_id
            or len(proposal.replacements) != 1
        ):
            raise AttestedTextReplacementError("replacement proposal is invalid")
        replacement = proposal.replacements[0]
        if not isinstance(replacement, TextReplacement):
            raise AttestedTextReplacementError("replacement proposal is invalid")
        if (
            not SAFE_PATH.fullmatch(replacement.path)
            or any(part in {"", ".", ".."} for part in replacement.path.split("/"))
            or not re.fullmatch(r"[0-9a-f]{64}", replacement.expected_sha256)
        ):
            raise AttestedTextReplacementError("replacement path is invalid")
        try:
            content = replacement.content.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise AttestedTextReplacementError("replacement content is invalid") from None
        if len(content) > MAX_FILE_BYTES or b"\x00" in content:
            raise AttestedTextReplacementError("replacement content is invalid")
        digest = hashlib.sha256(content).hexdigest()
        return (
            PayloadCommand(
                "/bin/sh",
                (
                    "-eu", "-c", REPLACEMENT_SCRIPT, "td-text-replacer",
                    replacement.path, replacement.expected_sha256, digest,
                ),
            ),
            content,
        )


def _proposal(task_id: str, before: bytes, after: str) -> TextMutationProposal:
    return TextMutationProposal(
        task_id, "1" * 40, "2" * 32, "probe",
        (
            TextReplacement(
                "docs/pilot.md", hashlib.sha256(before).hexdigest(), after
            ),
        ),
        2,
    )


def run_local_probe() -> None:
    with tempfile.TemporaryDirectory(
        prefix="td-mount-policy-", dir="/var/tmp"
    ) as temporary:
        root = Path(temporary)
        task, sibling = root / "task", root / "sibling"
        task.mkdir(mode=0o700)
        sibling.mkdir(mode=0o700)
        docs = task / "docs"
        docs.mkdir(mode=0o700)
        target = docs / "pilot.md"
        target.write_bytes(b"alpha\n")
        target.chmod(0o644)
        descriptor = os.open(task, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            handle = WorkspaceIdentityHandle(
                "ORCH-003D1B0I2", attempt=1, generation="0" * 32,
                descriptor=descriptor,
            )
        finally:
            os.close(descriptor)
        fixture = MountPolicyFixture(root, task, sibling)
        applier = AttestedTextReplacementApplier()
        try:
            applier.apply(
                fixture, handle,
                _proposal(handle.identity.task_id, b"alpha\n", "beta\n"),
            )
            require_definitive_rejection(
                lambda: applier.run(
                    fixture, handle,
                    _proposal(handle.identity.task_id, b"alpha\n", "stale\n"),
                )
            )
            real = task / "real"
            docs.rename(real)
            docs.symlink_to(real.name, target_is_directory=True)
            require_definitive_rejection(
                lambda: applier.run(
                    fixture, handle,
                    _proposal(handle.identity.task_id, b"beta\n", "alias\n"),
                )
            )
            docs.unlink()
            real.rename(docs)
            held = docs / "held.md"
            target.rename(held)
            target.mkdir(mode=0o700)
            require_definitive_rejection(
                lambda: applier.run(
                    fixture, handle,
                    _proposal(handle.identity.task_id, b"beta\n", "directory\n"),
                )
            )
            if tuple(target.iterdir()) or held.read_bytes() != b"beta\n":
                raise ReplacementOutcomeError("negative proof changed bytes")
            handle.verify()
        finally:
            handle.close()


if __name__ == "__main__":
    run_local_probe()
    print("Attested text replacement proof passed.")
