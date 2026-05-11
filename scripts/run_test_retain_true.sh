#!/usr/bin/env bash
# Test with jemalloc retain:true (DEFAULT on Linux 64-bit)
# Expects RSS to never decrease — memory creep should be visible.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULT_DIR="$DIR/results/retain_true_$TIMESTAMP"
mkdir -p "$RESULT_DIR"

echo "=============================================="
echo "TEST: jemalloc retain:true (DEFAULT)"
echo "Expected: RSS does NOT decrease after cleanup"
echo "Results: $RESULT_DIR"
echo "=============================================="

docker build -t streamlit-mem-test "$DIR"

docker run --rm \
  --memory 1200m \
  --memory-swap 1200m \
  -v "$RESULT_DIR:/app/results" \
  -e MALLOC_CONF="" \
  -e ARROW_DEFAULT_MEMORY_POOL=system \
  -e TEST_TTL=30 \
  -e TEST_MAX_ENTRIES=6 \
  -e TEST_NUM_PARAMS=4 \
  -e TEST_CYCLES=4 \
  -e TEST_DATA_MB=25 \
  -e TEST_USE_PYARROW=1 \
  streamlit-mem-test \
  python3 scripts/test_headless.py

echo ""
echo "Results saved to: $RESULT_DIR"
echo "Check headless_log.csv for RSS timeline."
echo ""
echo "Now run: scripts/run_test_retain_false.sh to compare."
