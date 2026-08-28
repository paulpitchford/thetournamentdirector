# ADR 0002: Use GitHub issues and pull requests as the control plane

- **Status:** Proposed
- **Date:** 2026-08-28

## Context

Agent work needs a visible approved queue, immutable integration record,
independent CI, review evidence, and protected merge boundary. The workspace
now has a clean local `main` baseline but no remote.

## Proposed decision

Use a private GitHub repository. Approved issues/tasks feed the local
controller. One task produces one named branch and one draft pull request.
Protected `main`, required CI, CODEOWNERS, and human approval control merge
throughout the pilot.

The host controller may push task branches and manage PR metadata using a
repository-scoped credential. Worker agents receive no GitHub write credential.

## Consequences

- PRs provide an auditable integration unit and quarantine boundary.
- Local controller state must reconcile with remote branch, issue, PR, and
  check state after restart.
- Human merge limits throughput but protects the experiment while evidence is
  gathered.
- Automatic merge, if ever considered, requires a later ADR and pilot data.

## Alternatives considered

- Local-only branches: simpler but cannot prove independent CI/PR governance.
- Agent-driven direct merges: rejected because prompts are not an acceptance
  boundary.
- Beads/Gas Town as primary task state: possible later, but adds another control
  plane before the GitHub workflow is understood.

## Approval needed

Choose the private GitHub repository owner/name and confirm human-only merge for
the whole pilot.
