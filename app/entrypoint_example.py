"""
Example entrypoint for a production Streamlit multipage app.
Shows the minimal required setup — Dockerfile env vars handle
all memory management. No Python-side daemon thread needed.

Place this in your app's entrypoint file — the one that calls pg.run().
"""

import streamlit as st

# =============================================================================
# Cached query functions
#
# Set max_entries to 2x your active parameter combinations.
# =============================================================================

@st.cache_data(ttl=300, max_entries=6)
def query_databricks(sql: str, params_hash: str):
    """Example cached Databricks query."""
    # result = connection.execute(sql, params)
    # polars_df = result.to_polars()
    # return polars_df.to_pandas()  # default: use_pyarrow_extension_array=True
    pass


# =============================================================================
# Page routing — goes below all function definitions
# =============================================================================

# pages = [...]
# pg = st.navigation(pages)
# pg.run()
