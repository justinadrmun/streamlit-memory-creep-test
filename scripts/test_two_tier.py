"""
Tests the two-tier converter: SAFE_TABLES whitelist vs runtime schema inspection.

Measures:
  - Runtime inspection overhead (is checking for nulls worth it?)
  - Memory savings for each table type
  - Whether runtime detection produces identical results to the whitelist
"""

import os, sys, time, gc, ctypes
import numpy as np
import polars as pl
import pandas as pd
import pyarrow as pa

sys.path.insert(0, "/app/app")
from polars_to_pandas import convert, SAFE_TABLES, generate_schema_report

# --- Allocator init ---
try:
    pa.jemalloc_set_decay_ms(0)
except Exception:
    pass
try:
    _libc = ctypes.CDLL("libc.so.6")
    _libc.malloc_trim.argtypes = [ctypes.c_int]
    _libc.malloc_trim.restype = ctypes.c_int
    def malloc_trim(pad=0): return _libc.malloc_trim(pad)
except Exception:
    def malloc_trim(pad=0): return -1

def get_rss_mb():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    return 0.0

def checkpoint(label=""):
    gc.collect()
    malloc_trim(0)
    time.sleep(0.5)
    rss = get_rss_mb()
    if label:
        print(f"  [rss] {label}: {rss:.0f} MB")
    return rss

# ====================================================================
# Mock Data: same 4 table types
# ====================================================================

ROWS = 400_000

def make_all_safe():
    rng = np.random.RandomState(1)
    return pl.DataFrame({
        "id": rng.randint(1, 1_000_000, ROWS),
        "sales": rng.rand(ROWS) * 10000,
        "cost": rng.rand(ROWS) * 8000,
        "margin": rng.rand(ROWS),
        "qty": rng.randint(1, 100, ROWS).astype(float),
        "region": rng.choice(["APAC", "EMEA", "AMER"], ROWS),
    })

def make_one_nullable_int():
    rng = np.random.RandomState(2)
    df = pl.DataFrame({
        "txn_id": rng.randint(1, 1_000_000, ROWS),
        "amount": rng.rand(ROWS) * 5000,
        "tax": rng.rand(ROWS) * 500,
        "fee": rng.rand(ROWS) * 100,
        "customer_id": rng.randint(100, 10000, ROWS),
    })
    mask = rng.rand(ROWS) < 0.10
    df = df.with_columns(
        pl.when(pl.lit(mask)).then(None).otherwise(pl.col("customer_id")).alias("customer_id")
    )
    return df

def make_has_decimal():
    rng = np.random.RandomState(3)
    df = pl.DataFrame({
        "item_id": rng.randint(1, 50000, ROWS),
        "units": rng.randint(1, 100, ROWS).astype(float),
        "weight": rng.rand(ROWS) * 50,
    })
    prices = (rng.rand(ROWS) * 999999).astype(np.int64)
    df = df.with_columns(
        pl.Series("price", prices).cast(pl.Decimal(precision=10, scale=2))
    )
    return df

def make_all_nullable_ints():
    rng = np.random.RandomState(4)
    df = pl.DataFrame({
        "a": rng.randint(1, 1000, ROWS),
        "b": rng.randint(1000, 2000, ROWS),
        "c": rng.randint(5000, 9999, ROWS),
        "d": rng.randint(1, 100, ROWS),
        "e": rng.randint(10000, 99999, ROWS),
    })
    for col in df.columns:
        mask = rng.rand(ROWS) < 0.15
        df = df.with_columns(
            pl.when(pl.lit(mask)).then(None).otherwise(pl.col(col)).alias(col)
        )
    return df

# ====================================================================
# Test: Schema inspection overhead
# ====================================================================

def test_inspection_overhead(n_iterations=20):
    """Measure how long runtime schema inspection takes."""
    print("\n" + "=" * 70)
    print("TEST A: Runtime schema inspection overhead")
    print("=" * 70)

    from polars_to_pandas import _inspect_schema

    for name, make_fn in [
        ("all_safe (pass)", make_all_safe),
        ("one_nullable_int (fail on first int)", make_one_nullable_int),
        ("has_decimal (fail on first col)", make_has_decimal),
        ("all_nullable_ints (fail after scanning)", make_all_nullable_ints),
    ]:
        df = make_fn()
        # Warm up
        _inspect_schema(df)
        # Measure
        times = []
        for _ in range(n_iterations):
            _, elapsed = _inspect_schema(df)
            times.append(elapsed * 1000)
        avg = sum(times) / len(times)
        classification = "SAFE → use False" if _inspect_schema(df)[0] else "UNSAFE → ArrowDtype"
        print(f"  {name}: avg {avg:.3f} ms → {classification}")


# ====================================================================
# Test B: Conversion — whitelist vs runtime detection vs baseline
# ====================================================================

def run_conversion(label, polars_df, approach, table_name, expected_int_cols):
    """Run one conversion and return (rss_delta, dtypes, timing, type_ok)."""
    import time as tmod

    print(f"  [{label}] {approach} for '{table_name}'")

    rss_before = get_rss_mb()
    t0 = tmod.perf_counter()

    if approach == "whitelist":
        pdf = polars_df.to_pandas(use_pyarrow_extension_array=False)
    elif approach == "runtime":
        pdf = convert(polars_df, table_name=None)  # no whitelist — forces inspection
    elif approach == "baseline":
        pdf = polars_df.to_pandas(use_pyarrow_extension_array=True)
    else:
        raise ValueError(approach)

    elapsed_ms = (tmod.perf_counter() - t0) * 1000

    # Type fidelity
    problems = []
    for col in expected_int_cols:
        if col in pdf.columns and pdf[col].dtype == np.float64:
            problems.append(col)
    if problems:
        print(f"    TYPE ERROR: {problems} coerced to float64")

    dtypes_sample = {k: str(v) for k, v in list(pdf.dtypes.items())[:3]}

    del pdf
    rss_after = checkpoint()
    delta = rss_after - rss_before

    print(f"    delta: {delta:+.0f} MB,  time: {elapsed_ms:.0f} ms,  dtypes: {dtypes_sample}")
    return delta, elapsed_ms, len(problems) == 0


def test_conversion():
    print("\n" + "=" * 70)
    print("TEST B: Conversion — whitelist vs runtime detection vs baseline")
    print("=" * 70)
    print("  (SAFE_TABLES currently contains: {})".format(set(SAFE_TABLES)))

    results = {}

    for name, make_fn, table_name, int_cols in [
        ("T1: all_safe", make_all_safe, "all_safe", frozenset()),
        ("T2: one_nullable_int", make_one_nullable_int, "one_nullable_int", frozenset({"customer_id"})),
        ("T3: has_decimal", make_has_decimal, "has_decimal", frozenset()),
        ("T4: all_nullable_ints", make_all_nullable_ints, "all_nullable_ints", frozenset({"a", "b", "c", "d", "e"})),
    ]:
        print(f"\n--- {name} ---")

        # Show what the converter would decide
        polars_df = make_fn()
        print(generate_schema_report(polars_df, table_name))
        del polars_df

        checkpoint()

        # Approach 1: baseline (ArrowDtype — type-safe default)
        polars_df = make_fn()
        d1, t1, ok1 = run_conversion(name, polars_df, "baseline", table_name, int_cols)
        del polars_df

        # Approach 2: runtime detection (no whitelist — forces schema inspection)
        polars_df = make_fn()
        d2, t2, ok2 = run_conversion(name, polars_df, "runtime", table_name, int_cols)
        del polars_df

        # Approach 3: whitelist equivalent (direct False — simulates SAFE_TABLES hit)
        polars_df = make_fn()
        d3, t3, ok3 = run_conversion(name, polars_df, "whitelist", table_name, int_cols)
        del polars_df

        results[name] = {
            "baseline_mb": d1, "runtime_mb": d2, "whitelist_mb": d3,
            "baseline_ms": t1, "runtime_ms": t2, "whitelist_ms": t3,
            "baseline_ok": ok1, "runtime_ok": ok2, "whitelist_ok": ok3,
        }

    # ====================================
    # Summary
    # ====================================
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"{'Table':<22} {'baseline':>8}  {'runtime':>8}  {'whitelist':>8}  {'inspect_ms':>8}  {'types_ok':>8}")
    print("-" * 70)

    for name, r in results.items():
        d1, d2, d3 = r["baseline_mb"], r["runtime_mb"], r["whitelist_mb"]
        t1, t2, t3 = r["baseline_ms"], r["runtime_ms"], r["whitelist_ms"]
        overhead = t2 - t3  # extra time from schema inspection
        types_ok = "OK" if r["runtime_ok"] else "BROKEN"

        print(f"{name:<22} {d1:+7.0f} MB  {d2:+7.0f} MB  {d3:+7.0f} MB  "
              f"{overhead:+7.1f} ms  {types_ok:>8}")
    
    print()
    print("  whitelist = directly calls use_pyarrow_extension_array=False")
    print("  runtime   = convert(df) → inspects schema then chooses path")
    print("  inspect_ms = runtime.ms - whitelist.ms (cost of null_count checks)")

    print(f"\nFinal RSS: {get_rss_mb():.0f} MB")


if __name__ == "__main__":
    os.makedirs("/app/results", exist_ok=True)

    print("=" * 70)
    print("TWO-TIER CONVERTER TEST")
    print(f"  SAFE_TABLES: {set(SAFE_TABLES)}")
    print(f"  ARROW_DEFAULT_MEMORY_POOL: {os.environ.get('ARROW_DEFAULT_MEMORY_POOL', '(default)')}")
    print("=" * 70)

    checkpoint("start")

    test_inspection_overhead()
    test_conversion()
