# ADR 0003: Treat coding agents as untrusted workers

- **Status:** Accepted
- **Date:** 2026-08-28

## Context

Agents can misunderstand instructions, follow prompt injection, weaken tests,
change unrelated files, leak available credentials, or produce plausible but
unsafe code. Reading policy documentation cannot be the security boundary.

## Decision

Treat every worker as an unreliable external contributor with shell access only
inside one disposable task worktree and hardened container.

Workers receive no personal GitHub token, SSH key, Docker socket, host home,
keyring, parent project mount, production credential, or vendor artifact. They
receive only a dedicated model credential and model-API egress required to run.
Trusted controller code enforces paths, protected files, budgets, gates, and PR
state from trusted `main`.

Use rootless Podman on the local pilot machine through its user-scoped,
Docker-compatible socket. Require no network by default, a read-only root
filesystem, dropped capabilities, `no-new-privileges`, a non-root container
user, and explicit process/memory/CPU limits. Do not solve local Docker socket
denial by making the host socket broadly writable; host Docker daemon access is
effectively root authority.

The reviewed verification script must prove the rootless socket and an isolated
no-op container before model dispatch. Moving to a dedicated runner requires a
later R3 decision.

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

The user approved local rootless execution on 2026-08-28. Rootless Podman and
its Docker-compatible user socket were then verified on the pilot host.
