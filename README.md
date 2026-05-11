# Streamlit Memory Creep Test

Reproduces and measures the memory creep pattern observed in a Streamlit dashboard running Polars → Pandas → `st.cache_data` (backed by `cachetools.TTLCache`) on jemalloc in a containerized environment.

## Quick Start (Docker)

```bash
# Build the image
docker build -t streamlit-mem-test .

# Test 1: jemalloc retain:true (default — expect creep)
./scripts/run_test_retain_true.sh

# Test 2: jemalloc retain:false (fix — expect RSS to decrease)
./scripts/run_test_retain_false.sh

# Run all 4 variants and compare
./scripts/run_full_comparison.sh
```

## Interactive Streamlit App (visual inspection)

```bash
docker compose up --build
# Open http://localhost:8501
# Use the buttons to run test cycles and observe RSS in real time
```

The Streamlit app shows:
- Live RSS chart (from background logger)
- Test log with per-cycle memory deltas
- Controls for TTL, max_entries, use_pyarrow_extension_array, data size
- Force GC / Clear Cache buttons

## What Tests Are Run

The headless test (`scripts/test_headless.py`) simulates the exact production pipeline:

1. **Per cycle:** Generate data → Polars DataFrame → convert to Pandas → store in `cachetools.TTLCache` (same library Streamlit uses for `st.cache_data`)
2. **Wait** for TTL expiry (default 30s)
3. **gc.collect()** — simulate Python GC
4. **cache.clear()** — simulate explicit cache eviction
5. **Measure RSS** at each phase

Each cycle uses **different parameter keys** (simulating different users querying different data), so TTL expiry does NOT trigger re-access eviction — the keys simply sit in the TTLCache dict.

## Four Test Variants

| Test | MALLOC_CONF | use_pyarrow_extension_array | Expected |
|------|-------------|-----------------------------|----------|
| 1 | (defaults) | True (zero-copy) | Worst: maximum creep |
| 2 | (defaults) | False (NumPy copy) | Creep reduced but present |
| 3 | retain:false | True (zero-copy) | Creep reduced, RSS may drop |
| 4 | retain:false | False (NumPy copy) | Best: RSS should return to baseline |

## Interpreting Results

After each cycle, the script prints:
```
[C1] Baseline: 150 MB
[C1] After fill: 280 MB (+130)
[C1] After TTL: 280 MB (+0 vs fill)
[C1] After GC: 270 MB (-10 vs fill)
[C1] After clear+GC: 220 MB (-60 vs fill)
[C1] STUCK: +70 MB
```

A **healthy allocator** should see `STUCK` near 0 MB by the final cycle after clear+GC.
**Memory creep** is confirmed when `STUCK` grows cycle-over-cycle and never returns to baseline.

## Kubernetes

```bash
# Run headless tests in k8s
kubectl apply -f k8s/deployment-retain-true.yaml
kubectl logs deployment/streamlit-mem-test-retain-true

kubectl apply -f k8s/deployment-retain-false.yaml
kubectl logs deployment/streamlit-mem-test-retain-false

# Run interactive Streamlit app in k8s
kubectl apply -f k8s/deployment-streamlit.yaml
kubectl port-forward svc/streamlit-mem-test-streamlit 8501:80
```

## Files

```
streamlit-memory-test/
├── Dockerfile                          # Chainguard Wolfi base + jemalloc
├── docker-compose.yml                  # Docker Compose with memory limits
├── requirements.txt                    # Python dependencies
├── app/
│   ├── main.py                         # Streamlit interactive test app
│   ├── rss_logger.py                   # Background RSS logger (CSV)
│   └── .streamlit/
│       └── config.toml                 # Streamlit server config
├── scripts/
│   ├── test_headless.py                # Automated test (no browser needed)
│   ├── run_test_retain_true.sh         # Docker test: retain:true
│   ├── run_test_retain_false.sh        # Docker test: retain:false
│   └── run_full_comparison.sh          # Run all 4 variants
└── k8s/
    ├── deployment-retain-true.yaml
    ├── deployment-retain-false.yaml
    └── deployment-streamlit.yaml
```

## Root Cause Summary

1. **`cachetools.TTLCache`** (used by Streamlit's `st.cache_data`): TTL eviction is lazy — entries are only removed when the specific key is accessed. Unaccessed keys sit in the dict forever (bounded by `max_entries`).
2. **jemalloc on Linux 64-bit**: `opt.retain=true` by default — jemalloc never calls `munmap()`. Decay settings (`dirty_decay_ms`, `muzzy_decay_ms`) only make pages available for internal reuse, never return them to the OS.
3. **Polars on Linux**: Allocates via the system allocator (jemalloc when `LD_PRELOAD` is set). Linux glibc-derived allocators retain significantly more memory than Windows/MSVC for the same workload.
4. **Polars→Pandas with `use_pyarrow_extension_array=True`**: Both frames share the same Arrow buffer. Deleting Polars doesn't free the buffer if Pandas still holds a reference. This creates dual-reference patterns that delay GC.
