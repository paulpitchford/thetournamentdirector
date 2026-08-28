# Agent orchestration research

Research snapshot: 2026-08-28.

## Goal

Create an unattended but controlled software-delivery loop that can turn an
approved plan into small changes, isolate concurrent agents, run deterministic
quality gates, open pull requests, obtain independent reviews, repair findings,
and keep reconciling work after individual agents or the host process fail.

“Unattended” must describe the controller, not unlimited agent authority. Every
agent run should be bounded. A durable controller may run continuously, but it
must stop dispatching when a safety, cost, quality, or ambiguity threshold is
crossed.

## Sandcastle

[Sandcastle](https://github.com/mattpocock/sandcastle) is Matt Pocock's MIT
licensed TypeScript library for running coding agents in isolated sandboxes. At
the time of this review its current package is `@ai-hero/sandcastle` 0.12.0.

Useful capabilities:

- TypeScript API rather than a fixed workflow product.
- Claude Code, Codex, Pi, Cursor, OpenCode, and Copilot agent providers.
- Docker, Podman, Vercel, and custom sandbox providers.
- Git worktrees and `head`, `merge-to-head`, or explicit branch strategies.
- Reusable sandboxes for implement-review or repair loops.
- Bounded iterations, idle/completion timeouts, cancellation, logs, and session
  capture/resume.
- Typed structured output validated with Zod or another Standard Schema tool.
- Prompt arguments and dynamic context commands. Prompt arguments are inert and
  do not execute embedded command syntax.
- Hooks for installing dependencies and preparing a sandbox.
- Seed templates including sequential review and parallel planning with review.

The `parallel-planner-with-review` template demonstrates:

1. An agent reads the issue backlog and emits a schema-validated dependency
   plan.
2. Implementers work on deterministic per-issue branches in parallel.
3. A reviewer runs after each implementation.
4. A merger agent combines completed branches.
5. The bounded outer loop repeats to pick up newly unblocked work.

### What Sandcastle does not provide by itself

Sandcastle is an execution substrate, not a complete software factory. Its
standard template is intentionally lightweight and does not supply all of the
controls wanted here:

- no durable queue, lease model, heartbeat, or crash-recovery state machine;
- no always-on supervisor;
- no built-in budget, retry, or failure circuit breaker policy;
- no authoritative PR/branch-protection policy;
- no project-specific deterministic QA gates;
- no separation between a read-only reviewer and a remediation agent;
- no default risk classification or human approval policy;
- no release/deployment policy;
- its planner template can merge branches directly instead of opening one PR
  per task.

The Sandcastle repository contains a `.factory/` adapter for an external
`factory daemon`, but the referenced `~/repos/ai/software-factory` implementation
is not in the public Sandcastle repository. It cannot be treated as an
available or reviewed component.

Conclusion: use Sandcastle as a candidate sandbox/agent adapter, not as the
source of truth for workflow state or merge approval.

## Other systems reviewed

| System | Strengths | Limit for this experiment |
|---|---|---|
| [GitHub Agentic Workflows (`gh-aw`)](https://github.com/github/gh-aw) | Markdown workflows compiled to Actions; several agent engines; read-only and sandboxed by default; controlled writes through validated `safe-outputs`; natural GitHub event integration | Best as a GitHub automation and safety layer, not a local long-running build controller; still requires carefully reviewed permissions and generated workflows |
| [Gas Town](https://github.com/gastownhall/gastown) | Persistent multi-agent work tracking, daemon/watchdogs, scheduling, escalation, worktrees, and a merge-queue “Refinery”; closest existing match to an always-on team | Considerably more infrastructure and terminology than one application needs: Town/Rigs, Beads, Dolt, tmux, Go services, and multiple agent roles |
| [OpenHands Agent Canvas](https://github.com/OpenHands/OpenHands) | Self-hosted control centre, multiple agent backends, schedules/webhooks, local/remote execution, UI and run history | Broader platform with several services; project-specific branch, gate, and merge policy still needs to be designed |
| [SWE-agent / mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent) | Focused, hackable issue-solving agent and strong benchmark/research lineage | Worker implementation rather than end-to-end orchestration, PR governance, or continuous operations |
| [Claude Code Action](https://github.com/anthropics/claude-code-action) | Mature GitHub issue/PR trigger, implementation, review, structured output, and provider options | Provider-specific workflow building block; not a durable multi-agent scheduler or complete QA policy |
| Managed background coding agents | Low setup and convenient PR production | Less control over sandbox details, durable workflow state, provider portability, and local proprietary artifacts; recurring service dependence |

## Recommendation

Run a staged experiment with a **thin repository-specific controller** and use
Sandcastle only for isolated agent execution.

- GitHub Issues/Projects: approved work queue and visible status.
- Local SQLite plus append-only run records: leases, attempts, budgets,
  heartbeats, and recovery.
- Sandcastle with Docker: one worktree and container per task.
- Host-side controller: the only component allowed to push, open PRs, apply
  labels, or request merge.
- GitHub Actions: independent, deterministic CI on every PR.
- Branch protection: authoritative required checks and human merge during the
  experiment.
- Optional `gh-aw`: later for read-only triage, CI-failure summaries, or safe
  repository reporting—not for the first mutating path.

This keeps the useful Sandcastle primitives while avoiding a direct merge loop
whose safety depends mostly on prompts.

Gas Town is worth a separate spike if maintaining our own scheduler and
watchdogs becomes the dominant work. It should not be adopted before a small
Sandcastle pilot establishes the actual throughput, failure modes, and costs of
this repository.

## Important workflow observations

### Continuous does not mean infinite

A reliable system uses an infinite **reconciliation loop** around finite jobs:

- every run has wall-clock, idle, token/cost, and iteration limits;
- every task has a retry limit;
- every review/repair cycle has a convergence limit;
- repeated infrastructure or test failures open a circuit breaker;
- ambiguous requirements and high-risk changes enter a human queue;
- the controller can resume leases after restart without asking an agent to
  remember state.

### Deterministic tools hold authority

Agents may propose plans, code, tests, review findings, and fixes. They must not
be the authority that decides their own work passed. Exit codes, schema
validation, branch protection, path policies, lockfile checks, test results,
and explicit approval rules make that decision.

### PRs should be the integration unit

The initial Sandcastle parallel template merges branches through another agent.
For this project, each task should instead produce a PR. This gives an audit
trail, independent CI, review comments, force-with-lease protection, and a
place to quarantine failures without contaminating the integration branch.

## Current workspace readiness

The present workspace cannot run the proposed PR workflow yet:

- it is not a Git repository;
- there is no configured remote or branch protection;
- `modern-app/` has not been scaffolded;
- Docker CLI is installed, but the current user cannot access the Docker daemon;
- `gh` is authenticated, but its broad personal token must not be passed into
  agent containers.

The ignored vendor directories are useful safety boundaries. They should remain
outside Git so agent worktrees never contain `downloads/`, `extracted/`, or
`analysis/decrypted/`.

## Sources

Primary material reviewed on 2026-08-28:

- Sandcastle repository, README, package metadata, templates, ADR inventory,
  `.factory/` adapter, and GitHub workflow examples:
  <https://github.com/mattpocock/sandcastle>
- GitHub Agentic Workflows README and security model:
  <https://github.com/github/gh-aw>
- Gas Town README and architecture:
  <https://github.com/gastownhall/gastown>
- OpenHands Agent Canvas README and architecture:
  <https://github.com/OpenHands/OpenHands>
- SWE-agent project guidance:
  <https://github.com/SWE-agent/SWE-agent>
- Claude Code Action README:
  <https://github.com/anthropics/claude-code-action>
- GitHub Actions secure-use guidance:
  <https://docs.github.com/en/actions/reference/security/secure-use>
- GitHub Security Lab on `pull_request_target` risks:
  <https://securitylab.github.com/resources/github-actions-preventing-pwn-requests/>

Repository popularity numbers were inspected only as a maintenance signal and
are deliberately not used as evidence of correctness or safety.
