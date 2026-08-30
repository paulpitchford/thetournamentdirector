# ADR 0009: Provision workspaces before exposing them to untrusted workers

- **Status:** Proposed
- **Date:** 2026-08-30

## Context

The controller needs a private task directory before it can materialize tracked
files, reserve a Git branch, or start a write-capable Codex worker. A normal
`mkdir` followed by `open` does not prove directory provenance if an adversarial
same-identity process can observe and mutate the parent namespace between those
operations. Device and inode checks after reopening do not repair that race.

The accepted worker boundary already requires a disposable container, a
non-root container user, and only the task worktree mounted into that container.
Workers must not receive the project parent, shared workspace root, controller
home, container socket, or controller-owned directory descriptors. The trusted
host controller and its deterministic helpers are inside the local pilot's TCB;
Codex workers and repository/task content are not.

PRs 17, 18, 20, 21, 22, 23, and 24 were quarantined rather than weakening this
boundary. The smaller merged identity handle proves only that a descriptor
supplied by a trusted provisioner can remain internally pinned and verified. It
does not create directories or authorize workers.

## Decision

Use a controller-owned provisioning phase that completes before the worker is
started:

1. The trusted controller opens a private workspace root that is absent from
   every worker mount namespace and container mount set.
2. While no untrusted process has access to the new task name or parent, the
   controller creates the generation-qualified direct child, opens it with
   no-follow directory semantics, validates ownership and private permissions,
   and pins it in the merged single-identity handle.
3. The controller records the task, attempt, generation, root identity, child
   identity, and active lease in durable state before materialization or worker
   dispatch.
4. A worker receives only its direct task directory through the reviewed
   container mount. It receives no parent/root path or descriptor and cannot
   enumerate or mutate sibling tasks.
5. The controller does not run a write-capable Codex process directly in the
   host mount namespace. Dispatch fails closed unless the root and sibling
   paths are demonstrably absent from the worker and the worker runs as the
   reviewed non-root container identity.
6. Cleanup and recovery are separate phases. Provisioning does not delete,
   reuse, or silently replace retained directories. Recovery must reconcile the
   durable identity and lease before any later namespace mutation.

The pilot treats unrelated processes running as the trusted host controller
account as part of the local TCB. This is not a claim that Unix mode bits isolate
mutually hostile processes with the same host identity. If that threat enters
scope, provisioning must move to a dedicated OS identity or dedicated runner
through a new R3 ADR.

## Required proof before write-capable dispatch

Deterministic tests must show that:

- injected worker commands cannot stat, list, open, rename, or watch the shared
  root or a sibling workspace;
- the worker can access only its mounted direct child;
- root, parent, and sibling descriptors are absent from inherited descriptors;
- root/path replacement after provisioning cannot redirect the pinned identity;
- startup refuses a broad parent mount, host-namespace write execution, a
  root-equivalent container user, or missing containment evidence;
- interruption retains bytes and durable identity for reconciliation rather
  than attempting pathname rollback.

A real rootless-container integration test is required in addition to mocked
systemd/command assertions. Passing unit tests alone is not sufficient evidence
for this boundary.

## Consequences

- Workspace provisioning and worker handoff remain separate bounded tasks.
- The first pilot stays single-worker until containment and recovery pass.
- Retained failed workspaces consume bounded storage until a reviewed
  reconciliation task handles them.
- A dedicated runner or OS identity remains an escalation path, not an implicit
  assumption.

## Alternatives considered

- **Authenticate a reopened path using only device and inode:** rejected because
  inode reuse and substitution remain possible when the original descriptor is
  not pinned.
- **Rely on a random pathname against a same-identity attacker:** rejected as a
  provenance boundary.
- **Export the shared root descriptor to the worker:** rejected because it grants
  sibling namespace authority.
- **Delete by pathname after descriptor validation:** rejected because the final
  pathname lookup can target a replacement.
- **Immediately require a privileged daemon:** deferred; the existing container
  boundary can satisfy the accepted threat model if the required proof passes.
