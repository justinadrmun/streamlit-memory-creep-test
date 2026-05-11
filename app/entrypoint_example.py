"""
Example entrypoint for a production Streamlit multipage app (8 pages).
Shows exactly where to place memory management initialization.

Place this in your app's entrypoint file — the one that calls pg.run().
"""

import os
import gc
import ctypes
import threading
import time

import streamlit as st
import pyarrow as pa

# =============================================================================
# PHASE 1: One-time memory allocator configuration
# Runs once per Session (each browser tab gets its own WebSocket session).
# The `st.cache_resource` guard prevents re-execution on st.rerun().
# =============================================================================

@st.cache_resource
def _init_memory_allocator():
    """Initialize memory allocator settings. Called exactly once per session.
    
    Uses @st.cache_resource (not @st.cache_data) because:
    - We want this to run ONCE per session, not on every rerun
    - cache_resource returns a singleton — won't re-execute after first call
    - This is the recommended pattern for one-time initialization in Streamlit
    """

    # --- Arrow jemalloc: force immediate page decay ---
    # Makes Arrow's internal jemalloc release dirty/muzzy pages to the OS
    # immediately rather than lazily. No effect on ongoing queries.
    try:
        pa.jemalloc_set_decay_ms(0)
    except Exception:
        pass  # Not available if Arrow isn't built with jemalloc

    # --- Start the malloc_trim daemon thread ---
    # glibc holds freed memory in per-thread arenas. malloc_trim(0) tells
    # glibc to release fully-free pages back to the OS.
    # Thread-safe (MT-Safe). Sub-millisecond call. No lock contention.
    #
    # Called every 60s — frequent enough to catch post-GC free pages,
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
        pass  # Not Linux / not glibc (e.g., macOS dev)


# =============================================================================
# PHASE 2: Call the initializer
# Must be at module level (not inside a function), before pg.run().
# =============================================================================

_init_memory_allocator()


# =============================================================================
# PHASE 3: Your cached query functions
#
# For functions that query Databricks and convert Polars -> Pandas:
# - Keep use_pyarrow_extension_array=True (the default) — preserves int nulls,
#   datetimes, categoricals correctly. Setting it to False breaks null ints.
# - The Arrow pool config (JE_ARROW_MALLOC_CONF) + malloc_trim thread handle
#   memory release. No need to sacrifice data integrity for memory.
# =============================================================================

@st.cache_data(ttl=300, max_entries=6)  # 6 = 2x your active param count
def query_databricks(sql: str, params_hash: str):
    """Example cached query function.

    Replace with your actual Databricks query logic.
    """
    # Your code:
    # result = connection.execute(sql, params)
    # polars_df = result.to_polars()
    # pandas_df = polars_df.to_pandas()  # use_pyarrow_extension_array=True (default)
    # return pandas_df
    pass


# =============================================================================
# PHASE 4: Page definitions and navigation
# This goes AFTER initialization, BEFORE pg.run()
# =============================================================================

# pages/ directory auto-discovery, or explicit:
# about_page = st.Page("pages/about.py", title="About")
# dashboard_page = st.Page("pages/dashboard.py", title="Dashboard")
# ... etc
# pg = st.navigation([about_page, dashboard_page, ...])
# pg.run()
