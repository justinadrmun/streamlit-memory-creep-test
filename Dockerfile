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

# --- Memory allocator configuration ---
# Route ALL Python allocations through jemalloc (disables pymalloc bypass)
ENV PYTHONMALLOC=malloc
# LD_PRELOAD jemalloc over glibc malloc
ENV LD_PRELOAD=/usr/lib/libjemalloc.so.2
# Glibc: limit per-thread memory arenas (prevents RSS bloat in containers)
ENV MALLOC_ARENA_MAX=2
# Glibc: release freed memory to OS when 128KB+ free at top of heap
ENV MALLOC_TRIM_THRESHOLD_=131072
# Glibc: use mmap for allocations >128KB instead of sbrk
ENV MALLOC_MMAP_THRESHOLD_=131072

# --- Arrow memory pool ---
# Use Arrow's own jemalloc with proper tuning (not "system")
ENV ARROW_DEFAULT_MEMORY_POOL=jemalloc
# Fix Arrow's jemalloc oversize_threshold:0 bug (arrow#46929)
# 8MB threshold = jemalloc default — enables reuse of large freed chunks
ENV JE_ARROW_MALLOC_CONF=oversize_threshold:8388608

# --- Polars mimalloc tuning (Polars bundles mimalloc in manylinux wheels) ---
# Reduce purge delay from 25s to 0ms — force immediate page release
ENV MIMALLOC_PURGE_DELAY=0
# Reset pages on free (return dirty pages to OS immediately)
ENV MIMALLOC_PAGE_RESET=1

EXPOSE 8501

CMD ["python3", "-m", "streamlit", "run", "app/main.py", \
    "--server.headless=true", \
    "--server.enableCORS=false", \
    "--server.enableXsrfProtection=false", \
    "--server.port=8501", \
    "--server.address=0.0.0.0"]
