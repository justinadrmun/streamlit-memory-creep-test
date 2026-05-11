#!/usr/bin/env bash
# Test with jemalloc retain:false
# Expects RSS to decrease after gc.collect() and cache.clear()
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULT_DIR="$DIR/results/retain_false_$TIMESTAMP"
mkdir -p "$RESULT_DIR"

echo "=============================================="
echo "TEST: jemalloc retain:false (FIX)"
echo "Expected: RSS DECREASES after cache.clear() + gc"
echo "Results: $RESULT_DIR"
echo "=============================================="

docker build -t streamlit-mem-test "$DIR"

docker run --rm \
  --memory 1200m \
  --memory-swap 1200m \
  -v "$RESULT_DIR:/app/results" \
  -e MALLOC_CONF="narenas:1,tcache:false,dirty_decay_ms:5000,muzzy_decay_ms:5000,background_thread:true,retain:false" \
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
echo ""
echo "Compare with retain:true results: diff <(tail -n1 results/retain_true_*/headless_log.csv) <(tail -n1 results/retain_false_*/headless_log.csv)"
