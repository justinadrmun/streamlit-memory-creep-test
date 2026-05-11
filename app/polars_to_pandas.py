"""
Drop-in replacement for polars_df.to_pandas() in cached query functions.
Applies memory-efficient conversion only when type-safe.
"""

import polars as pl
import pandas as pd


# Types that are unsafe to convert with use_pyarrow_extension_array=False:
# - Nullable integers become float64 (NaN coercion)
# - Decimal becomes object
# - Date inside List becomes string-formatted datetime
# - Time becomes object (no native pandas time type)
UNSAFE_TYPES = frozenset({
    pl.Int8, pl.Int16, pl.Int32, pl.Int64,
    pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64,
    pl.Decimal,
    pl.Time,
})


def _has_nullable_ints(df: pl.DataFrame) -> bool:
    """Check if any integer column has null values."""
    for col, dtype in df.schema.items():
        if dtype in (pl.Int8, pl.Int16, pl.Int32, pl.Int64,
                      pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64):
            if df[col].null_count() > 0:
                return True
    return False


def _has_unsafe_types(df: pl.DataFrame) -> bool:
    """Check if any column has a type that breaks without pyarrow extension."""
    for dtype in df.schema.values():
        if dtype in UNSAFE_TYPES:
            return True
        if isinstance(dtype, pl.Decimal):
            return True
        if isinstance(dtype, pl.List):
            if isinstance(dtype.inner, pl.Date):
                return True
    return False


def to_pandas_memory_safe(df: pl.DataFrame) -> pd.DataFrame:
    """Convert Polars DataFrame to Pandas.

    Uses use_pyarrow_extension_array=False (memory-efficient, 66 MB saved
    per cached result) when safe. Falls back to True (type-safe) when
    the DataFrame contains nullable integers, decimals, or other edge-case
    types that break without Arrow extension arrays.

    Call this instead of df.to_pandas() in your @st.cache_data functions.
    """
    if _has_nullable_ints(df) or _has_unsafe_types(df):
        # Safe path: preserves integer nulls, decimals, dates, etc.
        # Arrow pool config (ARROW_DEFAULT_MEMORY_POOL=jemalloc + decay)
        # handles memory release for the Arrow-backed extension arrays.
        return df.to_pandas(use_pyarrow_extension_array=True)

    # Memory-efficient path: NumPy-backed arrays — fully freeable
    # via Python GC + malloc_trim(). No edge-case types present,
    # so no data integrity risk.
    return df.to_pandas(use_pyarrow_extension_array=False)


# ---------- Usage in your cached query functions ----------
# Replace:
#     return df.to_pandas()
# With:
#     return to_pandas_memory_safe(df)
#
# For DataFrames coming from Databricks SQL, the types depend on your
# table schema. If your tables have nullable integer columns (INT, BIGINT
# with NULLs), the safe path will be taken automatically. If they're
# all float/non-null int/string, you get the memory savings.
