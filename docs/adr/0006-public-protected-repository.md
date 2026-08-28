# ADR 0006: Use a public repository with protected integration

- **Status:** Accepted
- **Date:** 2026-08-28
- **Supersedes:** the private-visibility choice in ADR 0002

## Context

ADR 0002 selected a private GitHub repository. GitHub Free does not provide
branch protection or repository rulesets for private repositories. Repository
privacy controls who can read content; it does not stop an authorized but buggy
controller credential from writing directly to `main`.

The Git history was checked before changing visibility. It contains no ignored
vendor binaries, recovered source, prohibited history paths, or common
secret-shaped values.

## Decision

Use the public repository
<https://github.com/paulpitchford/thetournamentdirector> and protect `main`.
Require pull requests, current-branch CI, linear history, resolved review
conversations, and CODEOWNER review for protected paths. Enforce protection for
administrators; reject force pushes and branch deletion. Enable secret scanning,
push protection, vulnerability alerts, and squash auto-merge.

The controller uses a repository-scoped deploy key that can push task branches
but cannot bypass protected `main`. Agents never receive that key.

## Consequences

- Authored source and documentation are publicly readable.
- Ignored proprietary artifacts must remain outside all Git objects and agent
  worktrees.
- GitHub supplies an integration boundary unavailable to a free private
  repository.
- A visibility change or weakening of branch rules is R3 and requires explicit
  user approval.
- A direct-push probe must continue to fail after material protection changes.

## Alternatives considered

- Private repository without protection: rejected for unattended mutation.
- GitHub Pro private repository: viable later, but not selected for this pilot.
- Local hooks as the merge boundary: rejected because authorized remote writers
  can bypass local hooks.
