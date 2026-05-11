"""Headless memory creep test. Runs in Docker without browser interaction.

Generates a CSV at /app/results/memory_log.csv and prints a summary.
"""

import os, sys, time, gc, threading
import numpy as np
import polars as pl
import pandas as pd
from cachetools import TTLCache
from datetime import datetime


def get_rss_mb():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    return 0.0


def generate_data(size_mb, seed=42):
    rng = np.random.RandomState(seed)
    n_rows = int(size_mb * 1024 * 1024 / (8 * 4))
    return pl.DataFrame({
        "id": rng.randint(1, 1000000, n_rows),
        "value_a": rng.randn(n_rows),
        "value_b": rng.randn(n_rows),
        "value_c": rng.randn(n_rows),
        "category": rng.choice(["A", "B", "C", "D", "E"], n_rows),
    })


def run_test(name, ttl, max_entries, num_params, cycles, data_size_mb,
             use_pyarrow_ext, csv_path):
    cache = TTLCache(maxsize=max_entries, ttl=ttl)
    results = []
    t0 = time.time()

    for c in range(cycles):
        cn = c + 1
        rss0 = get_rss_mb()

        params = [f"q{cn}_{i}" for i in range(num_params)]
        for p in params:
            key = p
            if key not in cache:
                seed = abs(hash(key)) % 10000
                polars_df = generate_data(data_size_mb, seed)
                pandas_df = polars_df.to_pandas(
                    use_pyarrow_extension_array=use_pyarrow_ext)
                del polars_df
                cache[key] = pandas_df

        rss1 = get_rss_mb()
        time.sleep(ttl + 3)
        rss2 = get_rss_mb()
        gc.collect()
        time.sleep(1)
        rss3 = get_rss_mb()
        cache.clear()
        gc.collect()
        time.sleep(1)
        rss4 = get_rss_mb()
        cache = TTLCache(maxsize=max_entries, ttl=ttl)

        results.append((cn, rss0, rss1, rss2, rss3, rss4))
        print(f"[{name}] C{cn}: {rss0:.0f} -> fill:{rss1:.0f} -> "
              f"ttl:{rss2:.0f} -> gc:{rss3:.0f} -> clear:{rss4:.0f} "
              f"(stuck: +{rss4-rss0:.0f} MB)")

        if c < cycles - 1:
            time.sleep(5)

    elapsed = time.time() - t0
    first_baseline = results[0][1]
    final_clear = results[-1][5]
    drift = final_clear - first_baseline

    with open(csv_path, "w") as f:
        f.write("timestamp,elapsed_s,rss_mb,event\n")
        evt_time = t0
        for r in results:
            cn, r0, r1, r2, r3, r4 = r
            f.write(f"{datetime.now().isoformat()},{evt_time-t0:.1f},{r0:.1f},cycle{cn}_baseline\n")
            f.write(f"{datetime.now().isoformat()},{evt_time-t0+1:.1f},{r1:.1f},cycle{cn}_fill\n")
            f.write(f"{datetime.now().isoformat()},{evt_time-t0+ttl+4:.1f},{r2:.1f},cycle{cn}_ttl\n")
            f.write(f"{datetime.now().isoformat()},{evt_time-t0+ttl+5:.1f},{r3:.1f},cycle{cn}_gc\n")
            f.write(f"{datetime.now().isoformat()},{evt_time-t0+ttl+6:.1f},{r4:.1f},cycle{cn}_clear\n")

    print(f"\n=== {name} SUMMARY ===")
    print(f"  Baseline RSS (cycle 1 start):  {first_baseline:.0f} MB")
    print(f"  Final RSS (after clear+GC):    {final_clear:.0f} MB")
    print(f"  Drift after {cycles} cycles:        +{drift:.0f} MB")
    print(f"  Elapsed: {elapsed:.0f}s")
    print(f"  MALLOC_CONF: {os.environ.get('MALLOC_CONF', '(default)')}")
    print(f"  use_pyarrow_extension_array: {use_pyarrow_ext}")
    print(f"  max_entries: {max_entries}")
    print()

    if drift > 50:
        print("RESULT: MEMORY CREEP CONFIRMED (>50MB drift)")
    elif drift > 20:
        print("RESULT: Moderate creep (>20MB drift)")
    else:
        print("RESULT: Memory stable (<20MB drift)")

    return drift


if __name__ == "__main__":
    os.makedirs("/app/results", exist_ok=True)

    TTL = int(os.environ.get("TEST_TTL", "30"))
    MAX_ENTRIES = int(os.environ.get("TEST_MAX_ENTRIES", "6"))
    NUM_PARAMS = int(os.environ.get("TEST_NUM_PARAMS", "4"))
    CYCLES = int(os.environ.get("TEST_CYCLES", "4"))
    DATA_MB = int(os.environ.get("TEST_DATA_MB", "25"))
    USE_PYARROW = os.environ.get("TEST_USE_PYARROW", "1") == "1"

    print("=" * 60)
    print("MEMORY CREEP TEST")
    print(f"  TTL: {TTL}s, max_entries: {MAX_ENTRIES}, params/cycle: {NUM_PARAMS}")
    print(f"  Cycles: {CYCLES}, data/query: ~{DATA_MB}MB")
    print(f"  use_pyarrow_extension_array: {USE_PYARROW}")
    print(f"  MALLOC_CONF: {os.environ.get('MALLOC_CONF', '(default)')}")
    print("=" * 60)

    drift_default = run_test(
        "default", TTL, MAX_ENTRIES, NUM_PARAMS, CYCLES, DATA_MB,
        USE_PYARROW, "/app/results/headless_log.csv")

    print(f"\nFinal drift: +{drift_default:.0f} MB")
    sys.exit(0 if drift_default < 50 else 1)
