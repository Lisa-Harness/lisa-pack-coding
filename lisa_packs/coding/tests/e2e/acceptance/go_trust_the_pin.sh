#!/usr/bin/env bash
# Run from the root of the fixture checkout, never from the scenario directory.
set -euo pipefail
scenario_acceptance=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_test="$scenario_acceptance/trust_the_pin_acceptance_test.go"
if [[ ! -f go.mod ]]; then
  printf '%s\n' 'go_trust_the_pin.sh must run in a Go fixture checkout' >&2
  exit 2
fi
# A noclobber claim avoids replacing a checkout file, even on a name collision.
while :; do
  copied_test="zz_lw_acceptance_${BASHPID}_${RANDOM}_test.go"
  if (set -o noclobber; : > "$copied_test") 2>/dev/null; then break; fi
done
cleanup() { rm -f -- "$copied_test"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cp -- "$source_test" "$copied_test"
# Respect an explicitly supplied GOCACHE; otherwise Go uses the user's cache.
# The module is stdlib-only; forbid proxy lookups and automatic toolchain fetches.
status=0
GOPROXY=off GOTOOLCHAIN=local go test -count=1 -run '^TestAcceptance' . || status=$?
exit "$status"
