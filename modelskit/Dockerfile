# =============================================================================
# AINA Translator – GPU-ready Docker image (CTranslate2)
# =============================================================================
FROM nvidia/cuda:12.1.0-runtime-ubuntu22.04

LABEL org.opencontainers.image.title="traduia-modelskit-cpu" \
      org.opencontainers.image.description="AINA translation microservice (Catalan to es/en/fr/pt/it/de), CPU build" \
      org.opencontainers.image.source="https://github.com/BSC-AppliedIntelligence/traduia" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV DEBIAN_FRONTEND=noninteractive

# System packages
RUN apt-get update && apt-get install -y --no-install-recommends --allow-change-held-packages \
        python3 \
        python3-pip \
        python3-dev \
        git \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3 /usr/bin/python

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .

# HuggingFace cache inside the container
ENV HF_HOME=/app/.hf_cache

# Expose the HTTP port
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
