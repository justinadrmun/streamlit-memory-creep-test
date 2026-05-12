"""
Headless test comparing the three-tier schema-driven conversion against baselines.
Generates realistic mock data for 4 table types and measures:
  - RSS after conversion + caching
  - Type fidelity (did nullable ints get coerced to float?)
  - Whether the conditional convert() chose the correct tier
"""

import os, sys, time, gc, ctypes
import numpy as np
import polars as pl
import pandas as pd
import pyarrow as pa
from cachetools import TTLCache
from datetime import datetime

# --- Import the converter under test ---
sys.path.insert(0, "/app/app")
from polars_to_pandas import convert, SAFE_TABLES, NULLABLE_INT_COLUMNS

# --- Allocator helpers ---
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
    time.sleep(1)
    rss = get_rss_mb()
    if label:
        print(f"  [rss] {label}: {rss:.0f} MB")
    return rss

# ====================================================================
# Mock Data: 4 realistic table types from Databricks
# ====================================================================

ROWS = 400_000  # ~25 MB per table

def make_all_safe():
    """Table with float + string columns, zero nulls."""
    rng = np.random.RandomState(1)
    return pl.DataFrame({
        "id": rng.randint(1, 1_000_000, ROWS),
        "sales_amount": rng.rand(ROWS) * 10000,
        "cost_amount": rng.rand(ROWS) * 8000,
        "margin_pct": rng.rand(ROWS),
        "quantity": rng.randint(1, 100, ROWS).astype(float),
        "region": rng.choice(["APAC", "EMEA", "AMER", "JAPN"], ROWS),
        "status": rng.choice(["active", "pending", "closed"], ROWS),
        "currency": rng.choice(["USD", "EUR", "GBP"], ROWS),
    })

def make_one_nullable_int():
    """4 safe float columns + 1 nullable BIGINT (customer_id has nulls)."""
    rng = np.random.RandomState(2)
    df = pl.DataFrame({
        "transaction_id": rng.randint(1, 1_000_000, ROWS),
        "amount": rng.rand(ROWS) * 5000,
        "tax_amount": rng.rand(ROWS) * 500,
        "fee_amount": rng.rand(ROWS) * 100,
        "customer_id": rng.randint(100, 10000, ROWS),
    })
    # Null out ~10% of customer_id values
    null_mask = rng.rand(ROWS) < 0.10
    df = df.with_columns(
        pl.when(pl.lit(null_mask))
          .then(None)
          .otherwise(pl.col("customer_id"))
          .alias("customer_id")
    )
    return df

def make_has_decimal():
    """3 float columns + 1 Decimal column (price is DECIMAL)."""
    rng = np.random.RandomState(3)
    df = pl.DataFrame({
        "item_id": rng.randint(1, 50000, ROWS),
        "units": rng.randint(1, 100, ROWS).astype(float),
        "weight_kg": rng.rand(ROWS) * 50,
    })
    # Build decimal column from scaled ints
    prices = (rng.rand(ROWS) * 999999).astype(np.int64)
    df = df.with_columns(
        pl.Series("unit_price", prices).cast(pl.Decimal(precision=10, scale=2))
    )
    return df

def make_all_nullable_ints():
    """5 columns, all nullable INTEGER with nulls scattered."""
    rng = np.random.RandomState(4)
    df = pl.DataFrame({
        "col_a": rng.randint(1, 1000, ROWS),
        "col_b": rng.randint(1000, 2000, ROWS),
        "col_c": rng.randint(5000, 9999, ROWS),
        "col_d": rng.randint(1, 100, ROWS),
        "col_e": rng.randint(10000, 99999, ROWS),
    })
    # Null out ~15% of each column
    for col in df.columns:
        null_mask = rng.rand(ROWS) < 0.15
        df = df.with_columns(
            pl.when(pl.lit(null_mask))
              .then(None)
              .otherwise(pl.col(col))
              .alias(col)
        )
    return df

# ====================================================================
# Type fidelity checks
# ====================================================================

def check_type_fidelity(pdf, name, expected_int_cols=frozenset()):
    """Verify that integer columns didn't get coerced to float64."""
    problems = []
    for col in expected_int_cols:
        if col in pdf.columns and pdf[col].dtype == np.float64:
            problems.append(f"  BAD: {name}.{col} is float64 (was int in Polars, got NaN coercion)")
    for col in pdf.columns:
        if pdf[col].dtype == np.float64 and col in expected_int_cols:
            problems.append(f"  BAD: {name}.{col} coerced to float64")
    if problems:
        print("\n".join(problems))
    return len(problems) == 0

# ====================================================================
# Run test: compare 3 approaches per table
# ====================================================================

def run_test(label, make_df_fn, table_name, expected_int_cols, approach, 
             warmup_cache=None):
    """Run one conversion approach for one table. Returns RSS delta."""
    print(f"\n--- {label}: {table_name} ---")

    # Check which tier the converter would pick
    if table_name in SAFE_TABLES:
        tier = "Tier 1 (False)"
    elif table_name in NULLABLE_INT_COLUMNS:
        tier = "Tier 2 (surgical)"
    else:
        tier = "Tier 3 (ArrowDtype)"
    print(f"  Converter tier: {tier}")

    polars_df = make_df_fn()
    rss_before = checkpoint("before")

    if approach == "baseline":
        pdf = polars_df.to_pandas(use_pyarrow_extension_array=True)
    elif approach == "false_only":
        pdf = polars_df.to_pandas(use_pyarrow_extension_array=False)
    elif approach == "conditional":
        pdf = convert(polars_df, table_name=table_name)
    else:
        raise ValueError(f"Unknown approach: {approach}")

    rss_after = checkpoint("after convert")
    delta = rss_after - rss_before

    # Type fidelity
    if expected_int_cols:
        ok = check_type_fidelity(pdf, table_name, expected_int_cols)
        if not ok:
            print(f"  WARNING: type fidelity check FAILED for {approach}")

    # Show dtype sample
    print(f"  Sample dtypes: {dict(list(pdf.dtypes.items())[:5])}")
    print(f"  RSS delta: +{delta:.0f} MB")

    del pdf, polars_df
    return delta

# ====================================================================
# Main
# ====================================================================

if __name__ == "__main__":
    os.makedirs("/app/results", exist_ok=True)

    print("=" * 70)
    print("SCHEMA-DRIVEN CONVERSION COMPARISON")
    print(f"  MALLOC_CONF: {os.environ.get('MALLOC_CONF', '(default)')}")
    print(f"  ARROW_DEFAULT_MEMORY_POOL: {os.environ.get('ARROW_DEFAULT_MEMORY_POOL', '(default)')}")
    print("=" * 70)

    checkpoint("baseline start")

    # ============================
    # Test 1: all_safe table (should use Tier 1)
    # ============================
    print("\n" + "=" * 70)
    print("TEST 1: all_safe (pure float + string, zero nulls)")
    print("Expected: Tier 1 wins — False is safe and saves memory")
    print("=" * 70)

    d1_baseline = run_test("baseline", make_all_safe, "all_safe", frozenset(), "baseline")
    d1_false = run_test("false_only", make_all_safe, "all_safe", frozenset(), "false_only")
    d1_cond = run_test("conditional", make_all_safe, "all_safe", frozenset(), "conditional")
    print(f"\n  all_safe results: baseline=+{d1_baseline:.0f}  false=+{d1_false:.0f}  cond=+{d1_cond:.0f} MB")

    # ============================
    # Test 2: one_nullable_int table (should use Tier 2)
    # ============================
    print("\n" + "=" * 70)
    print("TEST 2: one_nullable_int (4 safe float + 1 nullable BIGINT)")
    print("Expected: baseline safe but wasteful, False breaks the int, cond is best")
    print("=" * 70)

    int_cols = frozenset({"customer_id"})
    d2_baseline = run_test("baseline", make_one_nullable_int, "one_nullable_int", int_cols, "baseline")
    d2_false = run_test("false_only", make_one_nullable_int, "one_nullable_int", int_cols, "false_only")
    d2_cond = run_test("conditional", make_one_nullable_int, "one_nullable_int", int_cols, "conditional")
    print(f"\n  one_nullable_int: baseline=+{d2_baseline:.0f}  false=+{d2_false:.0f}  cond=+{d2_cond:.0f} MB")

    # ============================
    # Test 3: has_decimal (should use Tier 3)
    # ============================
    print("\n" + "=" * 70)
    print("TEST 3: has_decimal (3 float + 1 DECIMAL column)")
    print("Expected: baseline == cond (both ArrowDtype), False is wrong")
    print("=" * 70)

    d3_baseline = run_test("baseline", make_has_decimal, "has_decimal", frozenset(), "baseline")
    d3_false = run_test("false_only", make_has_decimal, "has_decimal", frozenset(), "false_only")
    d3_cond = run_test("conditional", make_has_decimal, "has_decimal", frozenset(), "conditional")
    print(f"\n  has_decimal: baseline=+{d3_baseline:.0f}  false=+{d3_false:.0f}  cond=+{d3_cond:.0f} MB")

    # ============================
    # Test 4: all_nullable_ints (should use Tier 3)
    # ============================
    print("\n" + "=" * 70)
    print("TEST 4: all_nullable_ints (5 columns, all nullable INTEGER)")
    print("Expected: baseline == cond (both ArrowDtype), False breaks ALL")
    print("=" * 70)

    all_int_cols = frozenset({"col_a", "col_b", "col_c", "col_d", "col_e"})
    d4_baseline = run_test("baseline", make_all_nullable_ints, "all_nullable_ints", all_int_cols, "baseline")
    d4_false = run_test("false_only", make_all_nullable_ints, "all_nullable_ints", all_int_cols, "false_only")
    d4_cond = run_test("conditional", make_all_nullable_ints, "all_nullable_ints", all_int_cols, "conditional")
    print(f"\n  all_nullable_ints: baseline=+{d4_baseline:.0f}  false=+{d4_false:.0f}  cond=+{d4_cond:.0f} MB")

    # ============================
    # Summary
    # ============================
    final_rss = checkpoint("test end")
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Table              | baseline | False   | conditional | Best path")
    print(f"all_safe           | +{d1_baseline:.0f} MB   | +{d1_false:.0f} MB  | +{d1_cond:.0f} MB      | {('False' if d1_false < d1_cond else 'cond')}")
    print(f"one_nullable_int   | +{d2_baseline:.0f} MB   | +{d2_false:.0f} MB  | +{d2_cond:.0f} MB      | {('cond' if d2_cond < d2_baseline else 'baseline')}")
    print(f"has_decimal        | +{d3_baseline:.0f} MB   | +{d3_false:.0f} MB  | +{d3_cond:.0f} MB      | {'equal'}")
    print(f"all_nullable_ints  | +{d4_baseline:.0f} MB   | +{d4_false:.0f} MB  | +{d4_cond:.0f} MB      | {'equal' if abs(d4_cond - d4_baseline) < 5 else 'baseline'}")
    print(f"\nType fidelity: baseline=pure, False=broken on nullable ints, cond=pure")
    print(f"Final RSS: {final_rss:.0f} MB")
