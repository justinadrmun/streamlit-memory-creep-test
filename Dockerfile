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

# Jemalloc library path for Wolfi
ENV LD_PRELOAD=/usr/lib/libjemalloc.so.2
ENV ARROW_DEFAULT_MEMORY_POOL=system

EXPOSE 8501

ENTRYPOINT ["python3", "-m", "streamlit", "run", "app/main.py", \
    "--server.headless=true", \
    "--server.enableCORS=false", \
    "--server.enableXsrfProtection=false", \
    "--server.port=8501", \
    "--server.address=0.0.0.0"]
