# ADR 0005: Deterministic gates control code acceptance

- **Status:** Proposed
- **Date:** 2026-08-28

## Context

Agent self-review and prose claims cannot establish that code is correct,
maintainable, secure, tested, or appropriately reusable. Review agents can also
miss defects or produce malformed findings.

## Proposed decision

A trusted policy manifest and canonical verification pipeline determine whether
a branch may become a ready PR. Required layers include strict TypeScript,
formatting, type-aware lint, architecture/cycle checks, dead-code and duplication
checks, unit/property/E2E tests, coverage/mutation ratchets, security and secret
scans, production build, independent structured code/security/QA reviews, and
bounded remediation.

Quality, workflow, package, lock, and critical invariant-test configuration is
protected from normal feature tasks. Critical/high findings block. Two failed
remediation cycles quarantine the task. Human approval remains mandatory.

## Consequences

- The scaffold and executable quality harness must precede unattended code.
- Tooling has maintenance and runtime cost.
- Bad fixtures must prove each important gate actually rejects violations.
- Metrics are review signals, not substitutes for domain invariants or human
  judgement.
- Changing a gate is an R3 task requiring human CODEOWNER approval.

## Alternatives considered

- Prompt-only coding standards: rejected because compliance is probabilistic.
- Lint and unit tests only: insufficient for architecture, security, weak tests,
  and semantic acceptance.
- Reviewer-agent approval as the merge gate: rejected because it is another
  probabilistic output.

## Approval needed

Confirm the quality policy and that initial setup cost is preferable to allowing
code agents before the gates are executable.
