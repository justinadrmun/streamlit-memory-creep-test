FROM cgr.dev/chainguard/wolfi-base:latest

RUN apk add --no-cache \
    python-3.12 \
    py3.12-pip \
    python-3.12-dev \
    jemalloc \
    jemalloc-dev \
    build-base \
    procps

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/app/
COPY scripts/ /app/scripts/

# =============================================================================
# Memory allocator configuration
# =============================================================================
# LD_PRELOAD jemalloc over glibc malloc (system-wide override)
ENV LD_PRELOAD=/usr/lib/libjemalloc.so.2
# Route ALL Python allocations through jemalloc (disables pymalloc — free to keep, no downside)
ENV PYTHONMALLOC=malloc

# =============================================================================
# Arrow memory pool (2nd biggest impact: saves ~73 MB in testing)
# =============================================================================
# Use Arrow's own jemalloc (namespaced symbols, no conflict with LD_PRELOAD)
ENV ARROW_DEFAULT_MEMORY_POOL=jemalloc
# Fix Arrow's jemalloc oversize_threshold:0 bug (arrow#46929)
# Without this, Arrow never reuses large freed chunks — VMS grows unbounded
ENV JE_ARROW_MALLOC_CONF=oversize_threshold:8388608

# =============================================================================
# Polars mimalloc tuning (modest impact: saves ~12 MB)
# =============================================================================
# Polars bundles mimalloc in manylinux wheels — these env vars control it
ENV MIMALLOC_PURGE_DELAY=0      # Default 25000ms → 0ms (immediate purge)
ENV MIMALLOC_PAGE_RESET=1       # Return dirty pages to OS on free

EXPOSE 8501

CMD ["python3", "-m", "streamlit", "run", "app/main.py", \
    "--server.headless=true", \
    "--server.enableCORS=false", \
    "--server.enableXsrfProtection=false", \
    "--server.port=8501", \
    "--server.address=0.0.0.0"]
