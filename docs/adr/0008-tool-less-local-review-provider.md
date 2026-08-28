# ADR 0008: Run local review models without repository tools

- **Status:** Proposed (staged implementation in progress)
- **Date:** 2026-08-28
- **Clarifies:** ADR 0003 and ADR 0007

## Context

A direct probe of Codex CLI's native `workspace-write` sandbox showed that a
model-generated command could read a file adjacent to its assigned workspace.
That sandbox alone therefore does not satisfy this repository's filesystem
boundary. Mounting the host's ChatGPT/Codex authentication into a container
would expose that credential to model-generated shell commands.

Code/security review and QA do not need direct repository tools when the trusted
controller supplies the exact task contract, current-SHA diff, deterministic
test output, and runtime evidence.

## Decision

Deliver this boundary in policy-sized stages: structured review contracts first,
then runtime/cgroup containment, then provider integration. No stage claims the
full adapter is enabled until all stages are accepted.

Run the authenticated Codex provider process locally with effectful capabilities
contained before execution. Copy Codex and its code-mode host into a
controller-owned temporary runtime directory while hashing both, verify the
staged Codex version, and execute that same staged copy. This binds execution
to the attested bytes rather than a replaceable source pathname.

Run the staged process as a transient systemd user service with
`KillMode=control-group`, a finite `RuntimeMaxSec`, forced final `SIGKILL`, and a
task limit. Systemd owns the service cgroup, so descendants remain in the
cleanup boundary even after `setsid()`. The controller stops and kills the unit
on every exit path. Then ignore user configuration and rules, disable shell,
browser, computer-use, apps,
MCP, and hosted web search, and select a named permission profile that grants
only minimal runtime reads with no workspace reads, writes, or network access.
Launch Codex with a minimal parent environment containing only `HOME`, locale,
and a controlled `PATH`; the file-backed provider authentication remains
available at its intended home location, while credential-bearing variables,
proxy settings, and unrelated host state are omitted. Inherit no additional
environment into tool processes and disable approvals. Run from an empty
temporary directory and pass only size-bounded inert JSON evidence through
standard input.

Reject any tool event, any process stderr, malformed JSONL, missing/distinct
session identity, stale SHA, malformed structured artifact, timeout, non-zero
exit, runtime attestation mismatch, cgroup setup/cleanup failure, or output
overflow. An exposed patch tool is
therefore denied by the permission profile before execution, and its diagnostic
also invalidates the review artifact.

The trusted controller runs deterministic QA/build commands separately inside
rootless Podman and supplies their bounded output to the tool-less QA agent.
Implementation and remediation still require the rootless worktree/container
boundary before they may run.

## Consequences

- The host Codex process can use the existing authenticated provider without
  copying its credential into a worker container.
- Untrusted diff/task text cannot cause filesystem or network effects because
  the pinned runtime enforces an empty workspace/network permission profile;
  event-stream and stderr validation are additional fail-closed checks.
- A live probe found that the permission profile alone does not contain hosted
  web search. Setting `web_search="disabled"` is therefore a mandatory,
  separately tested control.
- The final pinned-runtime probe exposed only patching, denied it before a write,
  exposed no filesystem-read or network tool, made no model network request,
  leaked no sentinel, and created no file.
- QA assesses controller-produced evidence rather than invoking commands itself.
- Review prompts and outputs remain subject to run, size, timeout, retention,
  session-separation, and current-SHA controls.
- No real model dispatch is enabled until adapter fixtures and an explicit
  tool-attempt probe pass.

## Alternatives considered

- Codex legacy `read-only` sandbox alone: rejected after the adjacent-file read
  probe because it permits broad filesystem reads.
- Mount host Codex authentication into a rootless container: rejected because a
  model-generated command could read the mounted credential.
- Give QA a host shell: rejected; deterministic commands belong in the rootless
  runner.
- Skip model review: rejected by ADR 0007.
