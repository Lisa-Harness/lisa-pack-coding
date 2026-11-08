#!/usr/bin/env bash
# Run from the root of the fixture checkout, never from the scenario directory.
set -euo pipefail
scenario_acceptance=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_test="$scenario_acceptance/nodesum_apply_discount.test.ts"
if [[ ! -f package.json || ! -f pnpm-lock.yaml ]]; then
  printf '%s\n' 'nodesum_apply_discount.sh must run in the nodesum checkout' >&2
  exit 2
fi
if [[ ! -d node_modules ]]; then
  printf '%s\n' 'node_modules is missing; prepare the workspace first' >&2
  exit 2
fi
# A noclobber claim avoids replacing a checkout file, even on a name collision.
while :; do
  copied_test="zz_nodesum_${BASHPID}_${RANDOM}.test.ts"
  if (set -o noclobber; : > "$copied_test") 2>/dev/null; then break; fi
done
cleanup() { rm -f -- "$copied_test"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cp -- "$source_test" "$copied_test"
# The installed demo package resolves from the worktree's own node_modules;
# Node runs the TypeScript directly through its built-in type stripping.
status=0
node --test "$copied_test" || status=$?
exit "$status"
