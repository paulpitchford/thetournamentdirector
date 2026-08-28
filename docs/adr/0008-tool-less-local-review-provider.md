# ADR 0008: Run local review models without repository tools

- **Status:** Accepted
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

Run the authenticated Codex provider process locally but expose **no model
tools**. Disable shell, browser, computer-use, search, apps, MCP/rules, and user
configuration for each ephemeral review invocation. Run from an empty temporary
directory and pass only size-bounded inert JSON evidence through standard input.
Reject any tool event, malformed JSONL, missing/distinct session identity, stale
SHA, malformed structured artifact, timeout, non-zero exit, or output overflow.

The trusted controller runs deterministic QA/build commands separately inside
rootless Podman and supplies their bounded output to the tool-less QA agent.
Implementation and remediation still require the rootless worktree/container
boundary before they may run.

## Consequences

- The host Codex process can use the existing authenticated provider without
  copying its credential into a worker container.
- Untrusted diff/task text cannot cause filesystem or network tool use because
  no model tool is available; event-stream validation is an additional
  fail-closed check.
- QA assesses controller-produced evidence rather than invoking commands itself.
- Review prompts and outputs remain subject to run, size, timeout, retention,
  session-separation, and current-SHA controls.
- No real model dispatch is enabled until adapter fixtures and an explicit
  tool-attempt probe pass.

## Alternatives considered

- Codex native sandbox alone: rejected after the adjacent-file read probe.
- Mount host Codex authentication into a rootless container: rejected because a
  model-generated command could read the mounted credential.
- Give QA a host shell: rejected; deterministic commands belong in the rootless
  runner.
- Skip model review: rejected by ADR 0007.
