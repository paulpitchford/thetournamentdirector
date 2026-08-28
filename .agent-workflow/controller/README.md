# Controller foundation

This package is trusted deterministic host code. It currently provides:

- strict loading of the approved pilot configuration;
- an atomic global pause switch;
- a durable SQLite run ledger;
- concurrency, daily-run, and review-reserve admission controls;
- mandatory fresh, distinct local sessions for code/security review and QA;
- an effect-contained local Codex review adapter with strict JSONL/artifact validation;
- a fake provider used to prove control flow without model quota or mutation.

It does **not** yet enable model dispatch, mutate a worktree, call GitHub, or run
as a daemon. Review models run locally with a hash-pinned Codex runtime, an
empty filesystem/network permission profile, a minimal allowlisted parent
process environment, no inherited tool environment, and hosted web search
disabled. Any exposed patch operation is denied before
execution; tool events and unexpected process diagnostics fail closed.
Deterministic commands run separately in rootless Podman. GitHub receives only
deterministic CI and controller-published status/evidence.

Run verification from the repository root:

```bash
.agent-workflow/scripts/verify_controller.sh
```

Exercise the fake provider without consuming model capacity:

```bash
PYTHONPATH=.agent-workflow/controller \
  python3 -m td_controller fake-run --task PILOT-FAKE-001 --role planning
```
