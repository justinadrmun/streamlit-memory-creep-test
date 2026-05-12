"""
Two-tier Polars → Pandas conversion with runtime schema inspection fallback.

Tier 1 (whitelist): If table_name is in SAFE_TABLES → use_pyarrow_extension_array=False.
                    Zero runtime overhead. Fastest path. Full memory savings.

Tier 2a (auto-detect safe): If table_name NOT in SAFE_TABLES, inspect the Polars schema.
                    If no nullable ints and no unsafe types → use_pyarrow_extension_array=False.
                    ~millisecond overhead, full memory savings.

Tier 2b (type-safe): If table has nullable ints or unsafe types →
                    to_arrow().to_pandas(types_mapper=...) — ArrowDtype-backed,
                    type-safe, faster than to_pandas(True).

Usage:
  from polars_to_pandas import convert
  result = convert(polars_df, table_name="stg_sales")
"""

from __future__ import annotations

import time
import polars as pl
import pandas as pd


# =============================================================================
# Whitelist — populate from your dbt/Databricks schema JSON.
# Tables listed here bypass runtime schema inspection entirely.
# =============================================================================

SAFE_TABLES: frozenset[str] = frozenset({
    # Populate from your dbt/Databricks schema JSON.
    # Tables listed here skip runtime inspection entirely.
    # "dim_customers",  # example: verified all float + string, no nullable ints
})


# =============================================================================
# Unsafe Polars types that force the ArrowDtype path.
# =============================================================================

_UNSAFE_TYPES = frozenset({
    pl.Decimal,
    pl.Time,
})

_NULLABLE_INT_TYPES = frozenset({
    pl.Int8, pl.Int16, pl.Int32, pl.Int64,
    pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64,
})


def _inspect_schema(polars_df: pl.DataFrame) -> tuple[bool, float]:
    """Check if the Polars DataFrame is safe for use_pyarrow_extension_array=False.

    Returns (is_safe, elapsed_seconds) so callers can measure overhead.
    A table is safe only if it has:
      - No nullable integer columns (nulls → float64 coercion)
      - No Decimal columns (→ object dtype)
      - No Time columns (→ object dtype)
      - No List-of-Date columns (→ string-formatted objects)
    """
    t0 = time.perf_counter()

    for col, dtype in polars_df.schema.items():
        # Check for Decimal (always unsafe)
        if isinstance(dtype, pl.Decimal):
            return False, time.perf_counter() - t0

        # Check for Time (always unsafe)
        if dtype in _UNSAFE_TYPES:
            return False, time.perf_counter() - t0

        # Check for List<Date> (date-in-list → object)
        if isinstance(dtype, pl.List) and isinstance(dtype.inner, pl.Date):
            return False, time.perf_counter() - t0

        # Check for nullable integers
        if dtype in _NULLABLE_INT_TYPES:
            if polars_df[col].null_count() > 0:
                return False, time.perf_counter() - t0

    return True, time.perf_counter() - t0


def convert(
    polars_df: pl.DataFrame,
    *,
    table_name: str | None = None,
) -> pd.DataFrame:
    """Convert Polars DataFrame to Pandas using the best available strategy.

    1. Whitelist hit → use_pyarrow_extension_array=False (zero overhead)
    2. Runtime inspection passes → use_pyarrow_extension_array=False (auto-detected)
    3. Runtime inspection fails → to_arrow().to_pandas(types_mapper=...) (type-safe)

    Args:
        polars_df: Polars DataFrame from query result.
        table_name: Optional table name for whitelist lookup.

    Returns:
        Pandas DataFrame ready for caching and display.
    """
    # Tier 1: whitelist — fast path, no inspection
    if table_name is not None and table_name in SAFE_TABLES:
        return polars_df.to_pandas(use_pyarrow_extension_array=False)

    # Tier 2: runtime inspection — auto-detect safety
    is_safe, _elapsed = _inspect_schema(polars_df)
    if is_safe:
        return polars_df.to_pandas(use_pyarrow_extension_array=False)

    # Tier 2b: type-safe ArrowDtype path
    arrow_table = polars_df.to_arrow()
    return arrow_table.to_pandas(
        types_mapper=lambda arrow_type: pd.ArrowDtype(arrow_type),
    )


# =============================================================================
# Schema report helper — run once to populate SAFE_TABLES
# =============================================================================

def generate_schema_report(polars_df: pl.DataFrame, table_name: str) -> str:
    """Print whether this table is safe and can be added to SAFE_TABLES."""
    is_safe, elapsed = _inspect_schema(polars_df)

    lines = [
        f"\nSchema report: {table_name}",
        f"  Inspection time: {elapsed*1000:.2f} ms",
        f"  Safe for use_pyarrow_extension_array=False: {is_safe}",
    ]
    if not is_safe:
        for col, dtype in polars_df.schema.items():
            if dtype in _NULLABLE_INT_TYPES and polars_df[col].null_count() > 0:
                lines.append(f"    Blocked by nullable int: {col} ({dtype}, {polars_df[col].null_count()} nulls)")
            elif isinstance(dtype, pl.Decimal):
                lines.append(f"    Blocked by Decimal: {col}")
            elif dtype == pl.Time:
                lines.append(f"    Blocked by Time: {col}")
            elif isinstance(dtype, pl.List) and isinstance(dtype.inner, pl.Date):
                lines.append(f"    Blocked by List<Date>: {col}")
    else:
        lines.append(f"    → ADD '{table_name}' to SAFE_TABLES for zero-overhead path")
    return "\n".join(lines)
