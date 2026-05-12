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

ENV LD_PRELOAD=/usr/lib/libjemalloc.so.2
ENV PYTHONMALLOC=malloc
ENV MALLOC_CONF="narenas:1,dirty_decay_ms:10000,muzzy_decay_ms:10000,background_thread:true,retain:false"
ENV ARROW_DEFAULT_MEMORY_POOL=jemalloc
ENV JE_ARROW_MALLOC_CONF=oversize_threshold:8388608
ENV MIMALLOC_PURGE_DELAY=0
ENV MIMALLOC_PAGE_RESET=1

EXPOSE 8501

CMD ["python3", "-m", "streamlit", "run", "app/main.py", \
    "--server.headless=true", \
    "--server.enableCORS=false", \
    "--server.enableXsrfProtection=false", \
    "--server.port=8501", \
    "--server.address=0.0.0.0"]
