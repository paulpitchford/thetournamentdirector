# Controller foundation

This package is trusted deterministic host code. It currently provides:

- strict loading of the approved pilot configuration;
- an atomic global pause switch;
- a durable SQLite run ledger;
- concurrency, daily-run, and review-reserve admission controls;
- a fake provider used to prove control flow without model quota or mutation.

It does **not** dispatch Codex, mutate a worktree, call GitHub, or run as a
daemon yet.

Run verification from the repository root:

```bash
.agent-workflow/scripts/verify_controller.sh
```

Exercise the fake provider without consuming model capacity:

```bash
PYTHONPATH=.agent-workflow/controller \
  python3 -m td_controller fake-run --task PILOT-FAKE-001 --role planning
```
