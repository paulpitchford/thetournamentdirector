#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(git rev-parse --show-toplevel)
export PYTHONPATH="${repo_root}/.agent-workflow/controller"

python3 -m compileall -q "${repo_root}/.agent-workflow/controller/td_controller"
python3 -m unittest discover \
  -s "${repo_root}/.agent-workflow/controller/tests" \
  -p 'test_*.py' \
  -v
python3 -m td_controller --config \
  "${repo_root}/.agent-workflow/policy/pilot.json" status >/dev/null

printf 'Controller verification passed.\n'
