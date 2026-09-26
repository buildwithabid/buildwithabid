#!/usr/bin/env bash
# Regenerates the samples with different random everyday spending and runs the
# detection checks on each set. Catches rules that only work on one lucky dataset.
#   bash tests/stress.sh 30
set -u
cd "$(dirname "$0")/.."
runs="${1:-20}"
tmp="$(mktemp -d)"
fail=0
for seed in $(seq 1 "$runs"); do
  SEED=$seed SAMPLES_OUT="$tmp/$seed" python3 tests/make-samples.py >/dev/null
  if out=$(SAMPLES_DIR="$tmp/$seed" node tests/run-detection.mjs 2>&1); then
    echo "seed $seed: all checks passed"
  else
    fail=$((fail + 1))
    echo "seed $seed: FAILED"
    echo "$out" | grep -E "^(WRONG|MISSED|EXTRA|FAIL) " | sed 's/^/    /'
  fi
done
rm -rf "$tmp"
echo "$fail of $runs random datasets had failures"
