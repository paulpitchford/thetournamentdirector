# Architecture decision records

ADRs record decisions that are costly to reverse or define a trust boundary.
They do not replace implementation plans or policy-as-code tests.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-agent-execution-substrate.md) | Codex/Sandcastle execution with Copilot PR review | Accepted |
| [0002](0002-github-control-plane.md) | Automated PR lifecycle with Copilot review | Accepted |
| [0003](0003-untrusted-agent-boundary.md) | Treat coding agents as untrusted workers | Proposed |
| [0004](0004-capacity-and-provider-routing.md) | Conservative provider capacity and routing | Proposed |
| [0005](0005-quality-gates.md) | Deterministic quality gates control acceptance | Accepted |

A proposed ADR becomes accepted only after the user approves its open choices.
Accepted ADRs are changed by a superseding ADR rather than silently rewritten.
