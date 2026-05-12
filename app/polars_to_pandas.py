"""
Schema-driven Polars → Pandas conversion for memory-efficient Streamlit caching.

Strategy (two tiers):
1. DataFrames with NO nullable ints/decimals → use_pyarrow_extension_array=False
   (NumPy-backed, fully freeable, saves ~66 MB per cached result)
2. DataFrames WITH nullable ints/decimals → df.to_arrow().to_pandas(types_mapper=...)
   (ArrowDtype-backed, type-safe, 4x faster than default to_pandas())

The decision is driven by a pre-computed schema whitelist generated from your dbt
project or Databricks Delta table metadata — no runtime inspection overhead.

Usage:
  from polars_to_pandas import convert
  result = convert(polars_df, table_name="stg_sales")
"""

from __future__ import annotations

import polars as pl
import pandas as pd


# =============================================================================
# SCHEMA WHITELIST — generate this from your dbt/Databricks schema
# =============================================================================
# Tables where ALL columns are safe for use_pyarrow_extension_array=False.
# "Safe" means: no nullable integers, no Decimal, no Time, no Date-in-List.
#
# To populate: check your dbt model schemas or run:
#   SELECT table_name, column_name, data_type, nullable
#   FROM information_schema.columns
#   WHERE table_schema = 'your_catalog.your_schema'
#
# Any table NOT listed here defaults to the type-safe ArrowDtype path.

SAFE_TABLES: frozenset[str] = frozenset({
    "all_safe",                # test table: pure float + string, zero nulls
    # "dim_customers",         # example — uncomment when verified safe
    # "fct_orders",            # example
})

# Tables with specific nullable-integer columns (partial unsafe).
# Column names here WILL become ArrowDtype-backed; all other columns
# get NumPy-backed for memory efficiency.
#
# If a table has nullable ints, list JUST those column names here.
# The converter will handle them surgically.

NULLABLE_INT_COLUMNS: dict[str, frozenset[str]] = {
    "one_nullable_int": frozenset({"customer_id"}),  # test table: 1 nullable int
    # "dim_customers": frozenset({"customer_id", "region_id"}),
    # "fct_orders": frozenset({"discount_code"}),
}


def generate_schema_report(
    polars_df: pl.DataFrame,
    table_name: str,
) -> str:
    """One-time helper: print the schema info needed to populate SAFE_TABLES
    and NULLABLE_INT_COLUMNS above. Run once per table, then hardcode results.

    Call this from a dev script or notebook to audit your schemas.
    """
    lines = [f"\nSchema report for: {table_name}"]
    has_unsafe_cols = False
    nullable_ints: list[str] = []

    for col, dtype in polars_df.schema.items():
        is_nullable_int = dtype in (
            pl.Int8, pl.Int16, pl.Int32, pl.Int64,
            pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64,
        ) and polars_df[col].null_count() > 0

        is_unsafe = any([
            is_nullable_int,
            isinstance(dtype, pl.Decimal),
            dtype == pl.Time,
            isinstance(dtype, pl.List) and isinstance(dtype.inner, pl.Date),
        ])

        if is_nullable_int:
            nullable_ints.append(col)
            has_unsafe_cols = True
        elif is_unsafe:
            has_unsafe_cols = True

        lines.append(f"  {col}: {dtype} {'← NULLABLE INT' if is_nullable_int else ''}")

    lines.append("")
    if not has_unsafe_cols:
        lines.append(f"  → ADD '{table_name}' to SAFE_TABLES")
    elif nullable_ints:
        cols = ", ".join(f'"{c}"' for c in nullable_ints)
        lines.append(f"  → ADD '{table_name}' to NULLABLE_INT_COLUMNS with: {cols}")
    else:
        lines.append(f"  → Table has unsafe types (Decimal/Time/Date-in-List). Use default path.")
    lines.append("")
    return "\n".join(lines)


# =============================================================================
# Conversion
# =============================================================================

def convert(
    polars_df: pl.DataFrame,
    *,
    table_name: str | None = None,
) -> pd.DataFrame:
    """Convert Polars DataFrame to Pandas, choosing the best strategy.

    1. Checks SAFE_TABLES whitelist → if safe, uses memory-efficient conversion
    2. Checks NULLABLE_INT_COLUMNS → converts safe columns with NumPy,
       nullable-int columns with ArrowDtype (surgical, not all-or-nothing)
    3. Falls back to type-safe ArrowDtype conversion (fast to_arrow() path)

    Args:
        polars_df: Polars DataFrame from Databricks query result.
        table_name: Optional table name to look up in whitelists.
                    If None, falls back to the type-safe path.

    Returns:
        Pandas DataFrame ready for caching and display.
    """
    if table_name is not None and table_name in SAFE_TABLES:
        # Tier 1: Full memory-efficient conversion.
        # All columns are pre-verified safe (no nullable ints, no Decimal, etc.)
        return polars_df.to_pandas(use_pyarrow_extension_array=False)

    if table_name is not None and table_name in NULLABLE_INT_COLUMNS:
        # Tier 2: Surgical — convert safe columns with NumPy,
        # nullable-int columns with ArrowDtype.
        nullable_cols = NULLABLE_INT_COLUMNS[table_name]
        return _convert_surgical(polars_df, nullable_cols)

    # Tier 3: Type-safe default.
    # Uses to_arrow().to_pandas(types_mapper=...) which is 4x faster than
    # to_pandas(use_pyarrow_extension_array=True) for large DataFrames
    # (Polars #24951). Same memory characteristics — ArrowDtype-backed.
    return _convert_arrow_fast(polars_df)


def _convert_arrow_fast(polars_df: pl.DataFrame) -> pd.DataFrame:
    """Type-safe conversion via Arrow table (fast path)."""
    arrow_table = polars_df.to_arrow()
    return arrow_table.to_pandas(
        types_mapper=lambda arrow_type: pd.ArrowDtype(arrow_type),
    )


def _convert_surgical(
    polars_df: pl.DataFrame,
    nullable_int_columns: frozenset[str],
) -> pd.DataFrame:
    """Convert: safe columns → NumPy, nullable-int columns → ArrowDtype.

    Splits the DataFrame, converts each group with the optimal strategy,
    and recombines. More memory-efficient than converting everything with
    ArrowDtype while preserving nullable integer semantics.
    """
    all_columns = polars_df.columns
    safe_cols = [c for c in all_columns if c not in nullable_int_columns]
    unsafe_cols = [c for c in all_columns if c in nullable_int_columns]

    if not safe_cols:
        return _convert_arrow_fast(polars_df)
    if not unsafe_cols:
        return polars_df.to_pandas(use_pyarrow_extension_array=False)

    # Convert groups separately
    safe_df = polars_df.select(safe_cols).to_pandas(
        use_pyarrow_extension_array=False,
    )
    unsafe_df = _convert_arrow_fast(polars_df.select(unsafe_cols))

    # Recombine — preserves column order from original
    result = pd.concat([safe_df, unsafe_df], axis=1)
    return result[all_columns]  # restore original order


# =============================================================================
# Schema discovery: run this against your Databricks warehouse to generate
# the SAFE_TABLES and NULLABLE_INT_COLUMNS entries above.
# =============================================================================

def discover_schemas_from_databricks(
    engine,
    catalog: str,
    database: str,
    table_filter: str = "%",
    skip_tables: tuple[str, ...] = (),
) -> str:
    """Query Databricks INFORMATION_SCHEMA and generate Python code for the
    SAFE_TABLES and NULLABLE_INT_COLUMNS whitelists above.

    Args:
        engine: SQLAlchemy engine connected to your Databricks warehouse.
        catalog: Databricks catalog name (e.g., 'main').
        database: Databricks schema/database name.
        table_filter: SQL LIKE pattern for table names. Default '%' = all.
        skip_tables: Table names to skip (e.g., staging tmp tables).

    Returns:
        Python code string to paste into this file.

    Example:
        code = discover_schemas_from_databricks(engine, "main", "prod")
        print(code)
        # Paste the output into SAFE_TABLES and NULLABLE_INT_COLUMNS above.
    """
    import re

    query = """
    SELECT
        table_name,
        column_name,
        data_type,
        is_nullable
    FROM {catalog}.information_schema.columns
    WHERE table_schema = :db
      AND table_name LIKE :tf
    ORDER BY table_name, ordinal_position
    """

    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            query.format(catalog=catalog),
            {"db": database, "tf": table_filter},
        ).fetchall()

    # Group by table
    tables: dict[str, list[tuple[str, str, str]]] = {}
    for table_name, column_name, data_type, is_nullable in rows:
        if table_name in skip_tables:
            continue
        tables.setdefault(table_name, []).append(
            (column_name, data_type, is_nullable),
        )

    safe_tables: list[str] = []
    nullable_int_entries: list[str] = []

    for table_name, columns in sorted(tables.items()):
        has_nullable_int = False
        nullable_cols: list[str] = []
        has_other_unsafe = False

        for col_name, data_type, is_nullable in columns:
            dt_upper = data_type.upper()
            is_int = bool(re.match(r"(TINY|SMALL|BIG)?INT", dt_upper))
            is_null = is_nullable.upper() == "YES"
            is_decimal = "DECIMAL" in dt_upper
            is_unsafe = ("DATE" in dt_upper or "TIME" in dt_upper)

            if is_int and is_null:
                has_nullable_int = True
                nullable_cols.append(col_name)
            if is_decimal or is_unsafe:
                has_other_unsafe = True

        if not has_nullable_int and not has_other_unsafe:
            safe_tables.append(table_name)
        elif has_nullable_int and not has_other_unsafe:
            cols_str = "frozenset({" + ", ".join(
                f'"{c}"' for c in nullable_cols
            ) + "})"
            nullable_int_entries.append(f'    "{table_name}": {cols_str},')

    lines = ["# --- Paste below into SAFE_TABLES ---", ""]
    if safe_tables:
        lines.append("SAFE_TABLES: frozenset[str] = frozenset({")
        for t in safe_tables:
            lines.append(f'    "{t}",')
        lines.append("})")
    else:
        lines.append("# No tables are fully safe.")

    lines.append("")
    lines.append("# --- Paste below into NULLABLE_INT_COLUMNS ---")
    if nullable_int_entries:
        lines.append("NULLABLE_INT_COLUMNS: dict[str, frozenset[str]] = {")
        lines.extend(nullable_int_entries)
        lines.append("}")
    else:
        lines.append("# No tables have isolated nullable integer columns.")

    return "\n".join(lines)
