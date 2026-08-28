# ADR 0003: Treat coding agents as untrusted workers

- **Status:** Proposed
- **Date:** 2026-08-28

## Context

Agents can misunderstand instructions, follow prompt injection, weaken tests,
change unrelated files, leak available credentials, or produce plausible but
unsafe code. Reading policy documentation cannot be the security boundary.

## Proposed decision

Treat every worker as an unreliable external contributor with shell access only
inside one disposable task worktree and hardened container.

Workers receive no personal GitHub token, SSH key, Docker socket, host home,
keyring, parent project mount, production credential, or vendor artifact. They
receive only a dedicated model credential and model-API egress required to run.
Trusted controller code enforces paths, protected files, budgets, gates, and PR
state from trusted `main`.

Prefer rootless Podman or a dedicated isolated runner. Do not solve local Docker
socket denial by making the socket broadly writable; Docker daemon access is
effectively host-root authority.

## Consequences

- Some convenient caches and host integrations cannot be mounted.
- A custom sandbox wrapper and egress proxy may be required.
- Agent GitHub actions are performed by the host controller after validation.
- Proprietary ignored directories remain absent from every Git worktree.
- Capability, policy, and governance tests must attempt known bypasses before
  unattended mutation.

## Alternatives considered

- Trust agent prompts and CLI permission mode: insufficient against mistakes or
  malicious repository/task content.
- Ordinary Docker-group access on the main workstation: convenient but expands
  host compromise impact.
- No sandbox for speed: rejected for unattended work.

## Approval needed

Choose local rootless Podman/rootless Docker versus a dedicated runner or VM.
