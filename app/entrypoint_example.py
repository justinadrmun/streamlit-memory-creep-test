"""
Example entrypoint for a production Streamlit multipage app (8 pages).
Shows exactly where to place memory management initialization.

Place this in your app's entrypoint file — the one that calls pg.run().
"""

import os, gc, ctypes, threading, time
import streamlit as st
import pyarrow as pa


# =============================================================================
# One-time memory allocator initialization.
# @st.cache_resource ensures this runs once per session, not on every rerun.
# =============================================================================

@st.cache_resource
def _init_memory():
    # Arrow jemalloc: force immediate page decay
    # Makes Arrow's internal jemalloc release dirty/muzzy pages immediately.
    # Thread-safe.  No effect on ongoing queries.
    try:
        pa.jemalloc_set_decay_ms(0)
    except Exception:
        pass

    # malloc_trim daemon: tells glibc to return free pages to the OS.
    # Thread-safe (MT-Safe per man page).  Sub-millisecond call.
    # Runs every 60s — frequent enough to catch post-GC free pages,
    # infrequent enough to have zero measurable overhead.
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim.argtypes = [ctypes.c_int]
        libc.malloc_trim.restype = ctypes.c_int

        def _trim_loop():
            while True:
                time.sleep(60)
                libc.malloc_trim(0)

        threading.Thread(target=_trim_loop, daemon=True, name="malloc-trim").start()
    except Exception:
        pass  # not Linux / not glibc (e.g., macOS dev)


_init_memory()


# =============================================================================
# Cached query functions
#
# Use polars_to_pandas.convert() instead of polars_df.to_pandas().
# It automatically chooses the best conversion path:
#   - use_pyarrow_extension_array=False when safe (saves ~66 MB per result)
#   - to_arrow().to_pandas(types_mapper=...) when unsafe (type-safe, fast)
# =============================================================================

from polars_to_pandas import convert


@st.cache_data(ttl=300, max_entries=6)
def query_databricks(sql: str, params_hash: str):
    """Example cached Databricks query."""
    # result = connection.execute(sql, params)
    # polars_df = result.to_polars()
    # return convert(polars_df)
    pass


# =============================================================================
# Page routing — goes AFTER initialization
# =============================================================================

# pages = [...]
# pg = st.navigation(pages)
# pg.run()
