import streamlit as st
import time, gc, os, subprocess, threading
import numpy as np
import polars as pl
import pandas as pd
from cachetools import TTLCache
from rss_logger import get_rss_mb, get_vms_mb, get_logger

st.set_page_config(page_title="Memory Leak Test", layout="wide")

# ---------- Environment info ----------
@st.cache_resource
def get_env_info():
    malloc_conf = os.environ.get("MALLOC_CONF", "(not set)")
    ld_preload = os.environ.get("LD_PRELOAD", "(not set)")
    arrow_pool = os.environ.get("ARROW_DEFAULT_MEMORY_POOL", "(not set)")
    jv = ""
    try:
        jv = subprocess.check_output(
            ["sh", "-c", "strings /usr/lib/libjemalloc.so.2 2>/dev/null | grep -m1 '^5\\.' || echo unknown"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        jv = "unknown"
    return {"malloc_conf": malloc_conf, "ld_preload": ld_preload,
            "arrow_pool": arrow_pool, "jemalloc": jv}

# ---------- Sidebar ----------
st.sidebar.header("Test Settings")
ttl = st.sidebar.number_input("TTL (seconds)", value=30, min_value=5, max_value=300, step=5)
max_entries = st.sidebar.number_input("max_entries", value=6, min_value=1, max_value=50, step=1)
use_pyarrow_ext = st.sidebar.checkbox("use_pyarrow_extension_array", value=True)
num_params = st.sidebar.slider("Param combos per cycle", 2, 8, 4)
data_size_mb = st.sidebar.slider("Data size per query (~MB)", 10, 80, 25)

env = get_env_info()
st.sidebar.divider()
st.sidebar.subheader("Environment")
for k, v in env.items():
    st.sidebar.text(f"{k}: {v}")
st.sidebar.divider()
rss_side = st.sidebar.empty()

# ---------- Data pipeline (mirrors production) ----------
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

_test_cache = TTLCache(maxsize=max_entries, ttl=ttl)
_cache_lock = threading.Lock()

def cached_query(param_key):
    logger = get_logger()
    with _cache_lock:
        if param_key in _test_cache:
            logger.mark_event(f"hit_{param_key}")
            return _test_cache[param_key]
    logger.mark_event(f"miss_{param_key}")
    seed = abs(hash(param_key)) % 10000
    polars_df = generate_data(data_size_mb, seed)
    pandas_df = polars_df.to_pandas(use_pyarrow_extension_array=use_pyarrow_ext)
    del polars_df
    gc.collect()
    with _cache_lock:
        _test_cache[param_key] = pandas_df
    logger.mark_event(f"store_{param_key}")
    return pandas_df

# ---------- Session state ----------
for k, v in [("test_on", False), ("test_log", []), ("test_cycles", 0)]:
    if k not in st.session_state:
        st.session_state[k] = v

# ---------- Main UI ----------
st.title("Streamlit Memory Creep Reproduction")
st.caption("Polars -> Pandas -> TTLCache (same stack as st.cache_data)")

c1, c2, c3, c4 = st.columns(4)
with c1:
    if st.button("Run 1 Cycle", disabled=st.session_state.test_on):
        st.session_state.test_on = True; st.session_state.test_cycles = 1; st.rerun()
with c2:
    if st.button("Run 3 Cycles", disabled=st.session_state.test_on):
        st.session_state.test_on = True; st.session_state.test_cycles = 3; st.rerun()
with c3:
    if st.button("Run 5 Cycles", disabled=st.session_state.test_on):
        st.session_state.test_on = True; st.session_state.test_cycles = 5; st.rerun()
with c4:
    if st.button("Force GC + Clear"):
        _test_cache.clear(); gc.collect()
        get_logger().mark_event("manual_clear"); st.rerun()

col_l, col_r = st.columns(2)
with col_l:
    st.subheader("Live RSS")
    rss_chart = st.empty()
with col_r:
    st.subheader("Test Log")
    log_area = st.empty()

# ---------- Run test cycles ----------
if st.session_state.test_on:
    logger = get_logger()
    n_cycles = st.session_state.test_cycles
    log = st.session_state.test_log

    for c in range(n_cycles):
        cn = c + 1
        logger.mark_event(f"cycle{cn}_start")
        rss0 = get_rss_mb()
        log.append(f"[C{cn}] Baseline: {rss0:.0f} MB")

        params = [f"p{cn}_{i}" for i in range(num_params)]
        for p in params:
            cached_query(p)

        rss1 = get_rss_mb()
        log.append(f"[C{cn}] After fill: {rss1:.0f} MB (+{rss1-rss0:.0f})")

        logger.mark_event(f"wait_ttl")
        log.append(f"[C{cn}] Waiting {ttl+5}s for TTL...")
        time.sleep(ttl + 5)

        rss2 = get_rss_mb()
        log.append(f"[C{cn}] After TTL: {rss2:.0f} MB ({rss2-rss1:+.0f} vs fill)")

        gc.collect(); time.sleep(1)
        rss3 = get_rss_mb()
        logger.mark_event(f"gc")
        log.append(f"[C{cn}] After GC: {rss3:.0f} MB ({rss3-rss1:+.0f} vs fill)")

        _test_cache.clear(); gc.collect(); time.sleep(1)
        rss4 = get_rss_mb()
        logger.mark_event(f"clear")
        log.append(f"[C{cn}] After clear+GC: {rss4:.0f} MB ({rss4-rss0:+.0f} vs baseline)")
        log.append(f"[C{cn}] STUCK: +{rss4-rss0:.0f} MB")

        if c < n_cycles - 1:
            log.append(f"--- waiting 10s ---")
            time.sleep(10)

    logger.mark_event("done")
    log.append("=== TEST DONE ===")
    st.session_state.test_log = log
    st.session_state.test_on = False
    st.rerun()

# ---------- Display ----------
log_text = "\n".join(st.session_state.test_log[-40:])
log_area.code(log_text or "Press a test button to start", language=None)

try:
    csv_path = "/app/results/memory_log.csv"
    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path)
        if not df.empty:
            rss_chart.line_chart(df.set_index("elapsed_s")["rss_mb"], height=300)
        else:
            rss_chart.info("Run a test to see the RSS chart.")
    else:
        rss_chart.info("No log data yet. Start a test.")
except Exception as e:
    rss_chart.warning(f"Chart not available: {e}")

rss_side.metric("RSS", f"{get_rss_mb():.0f} MB")
