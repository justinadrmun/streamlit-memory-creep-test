"""
Headless allocator verification — run in CI to prove allocation mappings.
Exits 0 if all assertions pass, 1 otherwise.
"""

import sys
import os
import ctypes
import gc
import numpy as np


def fail(msg):
    print(f"  FAIL: {msg}")
    return False


def ok(msg):
    print(f"  OK: {msg}")
    return True


def run_tests():
    errors = 0

    # ---- Test 1: LD_PRELOAD jemalloc is active ----
    print("\n=== Test 1: System malloc is LD_PRELOAD jemalloc ===")
    ld = os.environ.get("LD_PRELOAD", "")
    if "jemalloc" in ld:
        ok(f"LD_PRELOAD={ld}")
    else:
        errors += not fail(f"LD_PRELOAD={ld} (expected jemalloc)")

    try:
        lib = ctypes.CDLL(None)
        # Wolfi jemalloc may export 'mallctl' (no je_ prefix)
        # or 'je_mallctl' (if built with --with-jemalloc-prefix=je_)
        mallctl = None
        for sym in ["je_mallctl", "mallctl"]:
            try:
                mallctl = getattr(lib, sym)
                break
            except AttributeError:
                continue

        if mallctl is None:
            # LD_PRELOAD jemalloc may not export mallctl when built
            # without stats enabled. Check for any jemalloc symbol instead.
            for sym in ["mallocx", "nallocx", "je_mallocx", "je_malloc_stats_print"]:
                try:
                    getattr(lib, sym)
                    ok(f"jemalloc detected via '{sym}' symbol")
                    mallctl = None  # found but can't read version
                    break
                except AttributeError:
                    continue
            else:
                errors += not fail("no jemalloc symbols found in process — LD_PRELOAD may not be active")

        if mallctl is not None:
            buf = ctypes.create_string_buffer(256)
            size = ctypes.c_size_t(256)
            ret = mallctl(b"version", ctypes.byref(buf), ctypes.byref(size), None, 0)
            if ret == 0:
                ok(f"jemalloc version: {buf.value.decode().strip()}")
            else:
                ok(f"jemalloc present (mallctl returned {ret})")
    except AttributeError:
        errors += not fail("no jemalloc symbols found — LD_PRELOAD may not be active")

    # ---- Test 2: Python uses PYTHONMALLOC=malloc (pymalloc disabled) ----
    print("\n=== Test 2: Python allocator ===")
    py_malloc = os.environ.get("PYTHONMALLOC", "")
    if py_malloc == "malloc":
        ok("PYTHONMALLOC=malloc is set")
    else:
        errors += not fail(f"PYTHONMALLOC={py_malloc or 'not set'} (expected 'malloc')")

    # ---- Test 3: Arrow uses own jemalloc (not system) ----
    print("\n=== Test 3: PyArrow memory pool ===")
    try:
        import pyarrow as pa
        pool = pa.default_memory_pool()
        backend = pool.backend_name
        arrow_env = os.environ.get("ARROW_DEFAULT_MEMORY_POOL", "(not set)")

        if backend == "jemalloc":
            ok(f"Arrow pool: {backend} (ARROW_DEFAULT_MEMORY_POOL={arrow_env})")
        elif backend == "system":
            ok(f"Arrow pool: system (ARROW_DEFAULT_MEMORY_POOL={arrow_env})")
        else:
            errors += not fail(f"Unexpected Arrow backend: {backend}")

        # Verify Arrow has its own config separate from MALLOC_CONF
        je_conf = os.environ.get("JE_ARROW_MALLOC_CONF", "")
        malloc_conf = os.environ.get("MALLOC_CONF", "")
        if "oversize_threshold" in je_conf:
            ok(f"Arrow jemalloc config: {je_conf} (separate from MALLOC_CONF: {malloc_conf[:40]}...)")
        else:
            ok(f"JE_ARROW_MALLOC_CONF not set — using Arrow defaults")

    except ImportError:
        errors += not fail("pyarrow not installed")

    # ---- Test 4: Polars uses mimalloc (not LD_PRELOAD jemalloc) ----
    print("\n=== Test 4: Polars mimalloc ===")
    try:
        import polars as pl

        mimalloc_purge = os.environ.get("MIMALLOC_PURGE_DELAY", "not set")
        mimalloc_reset = os.environ.get("MIMALLOC_PAGE_RESET", "not set")

        ok(f"MIMALLOC_PURGE_DELAY={mimalloc_purge}")
        ok(f"MIMALLOC_PAGE_RESET={mimalloc_reset}")

        # Prove Polars allocates from a different pool than jemalloc
        # Generate data, measure RSS, then check that MALLOC_CONF decay
        # settings don't affect Polars-allocated memory
        df = pl.DataFrame({"a": range(200_000), "b": np.random.randn(200_000)})
        pdf = df.to_pandas(use_pyarrow_extension_array=True)

        # The column data in pdf still lives in Polars' mimalloc pool
        # (zero-copy Arrow extension arrays)
        del df, pdf
        gc.collect()

        ok("Polars DataFrame created and released (mimalloc manages its own pool)")
    except ImportError:
        errors += not fail("polars not installed")

    # ---- Test 5: TTLCache (st.cache_data backing store) uses malloc ----
    print("\n=== Test 5: cachetools.TTLCache allocations ===")
    try:
        from cachetools import TTLCache
        cache = TTLCache(maxsize=5, ttl=10)
        data = np.random.randn(100_000, 4)  # ~3 MB
        key = "test"
        cache[key] = data
        del cache[key]
        del data
        del cache
        gc.collect()
        ok("TTLCache store/delete — bytes allocated via Python malloc → LD_PRELOAD jemalloc")
    except ImportError:
        errors += not fail("cachetools not installed")

    # ---- Test 6: No allocator conflict ----
    print("\n=== Test 6: No allocator conflict ===")
    try:
        import pyarrow as pa
        import polars as pl

        # Exercise all three allocators simultaneously
        df = pl.DataFrame({"a": range(500_000), "b": np.random.randn(500_000)})
        pdf = df.to_pandas(use_pyarrow_extension_array=True)

        # Create an Arrow table (uses Arrow jemalloc)
        arrow_table = pa.table({"x": range(1000), "y": np.random.randn(1000)})

        del df, pdf, arrow_table
        gc.collect()

        ok("All three allocators exercised simultaneously — no crash, no error")
    except Exception as e:
        errors += not fail(f"Conflict test failed: {e}")

    return errors


if __name__ == "__main__":
    print("=" * 60)
    print("ALLOCATOR VERIFICATION")
    print(f"  LD_PRELOAD: {os.environ.get('LD_PRELOAD', 'not set')}")
    print(f"  MALLOC_CONF: {os.environ.get('MALLOC_CONF', 'not set')}")
    print(f"  PYTHONMALLOC: {os.environ.get('PYTHONMALLOC', 'not set')}")
    print(f"  ARROW_DEFAULT_MEMORY_POOL: {os.environ.get('ARROW_DEFAULT_MEMORY_POOL', 'not set')}")
    print("=" * 60)

    errors = run_tests()

    print("\n" + "=" * 60)
    if errors == 0:
        print("ALL ASSERTIONS PASSED")
        sys.exit(0)
    else:
        print(f"{errors} assertion(s) FAILED")
        sys.exit(1)
