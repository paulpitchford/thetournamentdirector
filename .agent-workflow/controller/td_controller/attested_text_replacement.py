"""Apply one validated text replacement inside the attested worker mount."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Protocol

from .attested_payload_runner import (
    AttestedPayloadRunner,
    AttestedPayloadRunnerError,
    PayloadCommand,
)
from .podman_mount_policy import MountPolicyFixture
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
resolved_parent=$(readlink -f "$parent")
test "$resolved_parent" = "$parent"
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
mv "$temporary" "$target"
trap - EXIT HUP INT TERM
if sync -f "$parent"; then
  printf 'text-replacement-ok\n'
else
  printf 'text-replacement-indeterminate\n'
  exit 52
fi
""".strip()


class ReplacementRunner(Protocol):
    def run(
        self,
        fixture: MountPolicyFixture,
        handle: WorkspaceIdentityHandle,
        payload: PayloadCommand,
        *,
        input_bytes: bytes = b"",
    ) -> ProcessOutput:
        ...


class AttestedTextReplacementError(RuntimeError):
    """Raised when a selected replacement is not applied exactly once."""


class AttestedTextReplacementIndeterminateError(
    AttestedTextReplacementError
):
    """Raised when recovery must reconcile whether bytes changed."""


class AttestedTextReplacementApplier:
    """Execute one preconditioned atomic replacement through the argv runner."""

    def __init__(self, *, runner: ReplacementRunner | None = None) -> None:
        if runner is not None and not callable(getattr(runner, "run", None)):
            raise AttestedTextReplacementError("replacement runner is invalid")
        self._runner = runner or AttestedPayloadRunner()

    def apply(
        self,
        fixture: MountPolicyFixture,
        handle: WorkspaceIdentityHandle,
        proposal: TextMutationProposal,
    ) -> None:
        """Apply exactly one replacement after inode, path, and digest checks."""
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
            raise AttestedTextReplacementError("replacement content is invalid") from exc
        if len(content) > MAX_FILE_BYTES or b"\x00" in content:
            raise AttestedTextReplacementError("replacement content is invalid")
        replacement_digest = hashlib.sha256(content).hexdigest()
        payload = PayloadCommand(
            "/bin/sh",
            (
                "-eu", "-c", REPLACEMENT_SCRIPT, "td-text-replacer",
                replacement.path, replacement.expected_sha256,
                replacement_digest,
            ),
        )
        try:
            output = self._runner.run(
                fixture, handle, payload, input_bytes=content
            )
        except AttestedPayloadRunnerError as exc:
            raise AttestedTextReplacementIndeterminateError(
                "replacement outcome requires reconciliation"
            ) from exc
        if (
            output.returncode in {1, 51}
            and not output.stdout
            and not output.stderr
        ):
            raise AttestedTextReplacementError("replacement was rejected")
        if (
            output.returncode != 0
            or output.stdout != b"text-replacement-ok\n"
            or output.stderr
        ):
            raise AttestedTextReplacementIndeterminateError(
                "replacement outcome requires reconciliation"
            )


def _proposal(task_id: str, path: str, before: bytes, after: str) -> TextMutationProposal:
    return TextMutationProposal(
        task_id, "1" * 40, "2" * 32, "probe",
        (TextReplacement(path, hashlib.sha256(before).hexdigest(), after),),
        2,
    )


def run_local_probe() -> None:
    with tempfile.TemporaryDirectory(
        prefix="td-mount-policy-", dir="/var/tmp"
    ) as temporary:
        root = Path(temporary)
        task = root / "task"
        sibling = root / "sibling"
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
                "ORCH-003D1B0I", attempt=1, generation="0" * 32,
                descriptor=descriptor,
            )
        finally:
            os.close(descriptor)
        fixture = MountPolicyFixture(root, task, sibling)
        applier = AttestedTextReplacementApplier()
        try:
            applier.apply(
                fixture, handle,
                _proposal(handle.identity.task_id, "docs/pilot.md", b"alpha\n", "beta\n"),
            )
            if target.read_bytes() != b"beta\n":
                raise AttestedTextReplacementError("replacement proof changed bytes")
            try:
                applier.apply(
                    fixture, handle,
                    _proposal(
                        handle.identity.task_id, "docs/pilot.md",
                        b"alpha\n", "stale\n",
                    ),
                )
            except AttestedTextReplacementError:
                pass
            else:
                raise AttestedTextReplacementError("stale digest was accepted")
            if target.read_bytes() != b"beta\n":
                raise AttestedTextReplacementError("stale proposal changed bytes")
            real = task / "real"
            docs.rename(real)
            docs.symlink_to(real.name, target_is_directory=True)
            try:
                applier.apply(
                    fixture, handle,
                    _proposal(handle.identity.task_id, "docs/pilot.md", b"beta\n", "gamma\n"),
                )
            except AttestedTextReplacementError:
                pass
            else:
                raise AttestedTextReplacementError("symlink parent was accepted")
            if (real / "pilot.md").read_bytes() != b"beta\n":
                raise AttestedTextReplacementError("rejected replacement changed bytes")
            handle.verify()
        finally:
            handle.close()


if __name__ == "__main__":
    run_local_probe()
    print("Attested text replacement proof passed.")
