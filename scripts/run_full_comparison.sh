#!/usr/bin/env bash
# Run all test variants and produce a comparison summary.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BASE_DIR="$DIR/results/comparison_$TIMESTAMP"
mkdir -p "$BASE_DIR"

echo "=============================================="
echo "FULL COMPARISON: 4 test variants"
echo "=============================================="

docker build -t streamlit-mem-test "$DIR"

declare -A TESTS
TESTS=(
  ["1_retain_true_pyarrow_true"]="MALLOC_CONF=\"\" TEST_USE_PYARROW=1"
  ["2_retain_true_pyarrow_false"]="MALLOC_CONF=\"\" TEST_USE_PYARROW=0"
  ["3_retain_false_pyarrow_true"]="MALLOC_CONF=\"narenas:1,tcache:false,dirty_decay_ms:5000,muzzy_decay_ms:5000,background_thread:true,retain:false\" TEST_USE_PYARROW=1"
  ["4_retain_false_pyarrow_false"]="MALLOC_CONF=\"narenas:1,tcache:false,dirty_decay_ms:5000,muzzy_decay_ms:5000,background_thread:true,retain:false\" TEST_USE_PYARROW=0"
)

SUMMARY=""

for name in "${!TESTS[@]}"; do
  echo ""
  echo "--- Running: $name ---"
  env_str="${TESTS[$name]}"
  env_str="$env_str TEST_TTL=30 TEST_MAX_ENTRIES=6 TEST_NUM_PARAMS=4 TEST_CYCLES=4 TEST_DATA_MB=25"

  mkdir -p "$BASE_DIR/$name"
  eval "docker run --rm --memory 1200m --memory-swap 1200m -v \"$BASE_DIR/$name:/app/results\" -e $env_str streamlit-mem-test python3 scripts/test_headless.py" 2>&1 | tee "$BASE_DIR/$name/output.txt"

  # Extract final drift line
  DRIFT_LINE=$(grep "Final drift" "$BASE_DIR/$name/output.txt" || echo "Unknown")
  SUMMARY="$SUMMARY\n$name: $DRIFT_LINE"
done

echo ""
echo "=============================================="
echo "COMPARISON SUMMARY"
echo "=============================================="
echo -e "$SUMMARY"
echo ""
echo "Detailed results: $BASE_DIR"
