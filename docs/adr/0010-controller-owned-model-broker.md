# ADR 0010: Keep model authentication in a tool-less controller broker

- **Status:** Accepted
- **Date:** 2026-08-30
- **Extends:** ADR 0003, ADR 0008, and ADR 0009

## Context

A write-capable worker needs model output, but placing the controller account's
Codex authentication in its container would expose a reusable credential to the
same boundary that executes untrusted model actions. Giving that container
ordinary outbound networking would also create an avoidable exfiltration path.
Repeated interactive authentication would make unattended operation unreliable.

The existing review adapter already proves a narrower pattern: an attested local
Codex process can use the controller's existing login while receiving only a
bounded prompt, exposing no tools, and running in a killable transient cgroup.
The rootless worker container can separately perform filesystem effects without
credentials or model networking.

## Decision

Separate model inference from worker effects:

1. A controller-owned, tool-less model broker runs the pinned Codex process
   locally. It retains normal Codex authentication and refresh behavior; login
   files are never copied or mounted into a worker container.
2. The broker receives only controller-built, size-bounded inputs. It has no
   repository checkout and rejects shell, filesystem, network, web, MCP,
   browser, patch, and planning tool events.
3. Broker output must match a closed schema and complete the strict JSONL
   lifecycle in a fresh session. Errors and diagnostics are normalized before
   entering durable evidence.
4. Later implementation requests will contain only controller-selected tracked
   file bytes and task data. The broker returns a declarative mutation proposal,
   not executable commands.
5. The controller validates that proposal against the task contract. Only the
   rootless, credential-free, network-disabled worker container may materialize
   accepted effects in its direct task mount.
6. Test output and current diffs return to fresh tool-less remediation, review,
   and QA sessions through the same bounded broker boundary.

The broker is not a general HTTP proxy and does not grant worker network access.
A fixed schema-bound smoke request must pass before mutation proposals are
implemented.

## Consequences

- Existing Codex login normally refreshes without repeated operator action.
- Credentials and unrestricted provider networking remain outside workers.
- Model context must be selected and bounded by the controller, so implementation
  may require several proposal/remediation rounds instead of direct repository
  exploration.
- Applying mutations, running tests, Git operations, and publishing evidence
  remain separate deterministic controller responsibilities.
- A future direct-tool worker would require a new R3 ADR and a dedicated
  credential/egress boundary; this decision does not silently permit one.

## Alternatives considered

- **Mount the controller's Codex login into the worker:** rejected because model
  actions could read or exfiltrate a reusable credential.
- **Give the worker ordinary outbound internet access:** rejected because it is
  broader than provider inference and unnecessary for deterministic effects.
- **Require interactive login for each run:** rejected because it prevents
  unattended operation and does not improve per-run containment.
- **Run write-capable Codex directly in the host namespace:** rejected because it
  bypasses the accepted workspace boundary.
