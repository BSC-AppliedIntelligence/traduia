# AINA Translator Microservice

<div align="center">

**Real-time translation from Catalan to multiple languages**

[![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=flat&logo=fastapi)](https://fastapi.tiangolo.com)
[![CTranslate2](https://img.shields.io/badge/CTranslate2-4.0+-blue)](https://github.com/OpenNMT/CTranslate2)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com)
[![GPU](https://img.shields.io/badge/GPU-CUDA%20enabled-76B900?logo=nvidia&logoColor=white)](https://developer.nvidia.com/cuda-zone)

High-performance translation microservice powered by [Projecte AINA](https://projecte-aina.github.io/) CTranslate2 models.

[Features](#-features) • [Quick Start](#-quick-start) • [API Reference](#-api-reference) • [Docker](#-docker) • [Performance](#-performance)

</div>

---

## 🎯 Overview

AINA Translator is a production-ready FastAPI microservice that provides real-time translation from Catalan to 6 target languages. Built with CTranslate2 for maximum performance, it achieves **sub-second translation latency** on GPU and supports parallel multi-language translation.

### Supported Languages

| Source | Targets |
|--------|---------|
| **Catalan** (`ca`) | English, Spanish, French, Portuguese, Italian, German |
|  | `en`, `es`, `fr`, `pt`, `it`, `de` |

---

## ✨ Features

- 🚀 **High Performance**: Sub-second translation latency with CTranslate2 optimization
- 🔄 **Parallel Translation**: Translate to multiple languages simultaneously
- 🎮 **GPU Accelerated**: Automatic CUDA detection with float16 precision
- 🔌 **REST API**: Clean FastAPI interface with automatic OpenAPI docs
- 🐳 **Docker Ready**: Containerized deployment with GPU support
- ⚡ **Async Architecture**: ThreadPoolExecutor for concurrent translations
- 🎯 **Production Ready**: Health checks, validation, and comprehensive error handling
- 📊 **Configurable**: Beam search, max tokens, and worker pool customization

---

## 🛠️ Tech Stack

- **Framework**: FastAPI 0.104+
- **Translation Engine**: CTranslate2 4.0+
- **Tokenization**: PyOnmttok 1.37+ (SentencePiece)
- **Models**: Projecte AINA (Hugging Face)
- **Server**: Uvicorn with async workers
- **Container**: Docker with NVIDIA GPU support

---

## 🚀 Quick Start

### Prerequisites

- Python 3.10+
- CUDA 11+ (optional, for GPU acceleration)
- 8GB+ RAM (16GB+ recommended for all models)
- Docker (optional, for containerized deployment)

### Local Installation

```bash
# Clone the repository
git clone https://github.com/BSC-AppliedIntelligence/traduia.git
cd traduia/modelskit

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the service
uvicorn app:app --host 0.0.0.0 --port 8000
```

The service will download models on first startup (~2GB total). This takes 2-5 minutes depending on your connection.

### Docker Installation

```bash
# Build the GPU image (see the Docker section) or use the published one
docker build -f Dockerfile.gpu.ct2-source -t traduia-modelskit .

# Run with GPU support
docker run --gpus all -p 8000:8000 traduia-modelskit

# CPU-only: use the plain image
docker build -t traduia-modelskit-cpu . && docker run -p 8000:8000 traduia-modelskit-cpu
```

### Verify Installation

```bash
# Check health
curl http://localhost:8000/health

# Test translation
curl -X POST http://localhost:8000/translate \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Bon dia, com estàs?",
    "targets": ["en", "es"]
  }'
```

---

## 📚 API Reference

### Base URL

```
http://localhost:8000
```

### Endpoints

#### `GET /`

Root endpoint with service information.

**Response:**
```json
{
  "service": "AINA Translator",
  "version": "2.0.0",
  "engine": "CTranslate2",
  "docs": "/docs",
  "health": "/health"
}
```

---

#### `GET /health`

Health check endpoint showing service status and loaded models.

**Response:**
```json
{
  "status": "ok",
  "device": "cuda",
  "models_loaded": ["en", "es", "fr", "pt", "it", "de"]
}
```

---

#### `POST /translate`

Translate Catalan text to one or more target languages.

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | Yes | Catalan source text (min 1 char) |
| `targets` | array | Yes | List of target language codes |
| `max_decoding_length` | integer | No | Max tokens to generate (default: 256, max: 1024) |
| `beam_size` | integer | No | Beam search width (default: 1, max: 10) |

**Example Request:**
```json
{
  "text": "La intel·ligència artificial està transformant el món",
  "targets": ["en", "es", "fr"],
  "beam_size": 1,
  "max_decoding_length": 256
}
```

**Example Response:**
```json
{
  "en": "Artificial intelligence is transforming the world",
  "es": "La inteligencia artificial está transformando el mundo",
  "fr": "L'intelligence artificielle transforme le monde"
}
```

**Notes:**
- Unknown language codes return `null`
- All translations run in parallel
- Response time scales with number of targets

---

## 🎨 Usage Examples

### Python

```python
import requests

url = "http://localhost:8000/translate"

# Single language
response = requests.post(url, json={
    "text": "Avui fa molt bon temps",
    "targets": ["en"]
})
print(response.json())
# {"en": "Today the weather is very nice"}

# Multiple languages
response = requests.post(url, json={
    "text": "M'agrada programar en Python",
    "targets": ["en", "es", "fr"]
})
print(response.json())
# {
#   "en": "I like programming in Python",
#   "es": "Me gusta programar en Python",
#   "fr": "J'aime programmer en Python"
# }

# With beam search for better quality
response = requests.post(url, json={
    "text": "El president va anunciar noves mesures econòmiques",
    "targets": ["en"],
    "beam_size": 4
})
print(response.json())
```

### JavaScript

```javascript
const response = await fetch('http://localhost:8000/translate', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    text: 'Barcelona és una ciutat increïble',
    targets: ['en', 'es']
  })
});

const data = await response.json();
console.log(data);
// {
//   "en": "Barcelona is an incredible city",
//   "es": "Barcelona es una ciudad increíble"
// }
```

### cURL

```bash
# Translate to all languages
curl -X POST http://localhost:8000/translate \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Hola, necessito ajuda amb aquest projecte",
    "targets": ["en", "es", "fr", "pt", "it", "de"]
  }'
```

---

## ⚡ Performance

### Benchmark Results (RTX 5090)

| Test | Languages | Latency | Throughput |
|------|-----------|---------|------------|
| Single (en) | 1 | 0.15s | ~6.7 req/s |
| Triple (en, es, fr) | 3 | 0.28s | ~3.6 req/s |
| All languages | 6 | 0.45s | ~2.2 req/s |

### Optimization Tips

**GPU Configuration**
```python
# In app.py, adjust for your hardware
MAX_WORKERS = 6  # Increase for more parallel capacity
compute_type = "float16"  # Use "int8" for faster inference
```

**Environment Variables**
```bash
export TRANSLATE_WORKERS=8  # Concurrent translation threads
export CUDA_VISIBLE_DEVICES=0  # Select GPU
```

**Beam Search Trade-offs**
- `beam_size=1`: Fastest, greedy decoding
- `beam_size=4`: Better quality, 2-3x slower
- `beam_size=8`: Best quality, 4-5x slower

---

## 🔧 Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TRANSLATE_WORKERS` | `6` | ThreadPoolExecutor max workers |
| `CUDA_VISIBLE_DEVICES` | - | GPU selection (e.g., `0,1`) |

### Model Registry

Models are defined in `app.py`:

```python
MODEL_REGISTRY = {
    "en": {"repo": "projecte-aina/aina-translator-ca-en", "spm": "spm.model"},
    "es": {"repo": "projecte-aina/aina-translator-ca-es", "spm": "spm.model"},
    # ... add more languages
}
```

To add a new language pair:
1. Add entry to `MODEL_REGISTRY`
2. Ensure CTranslate2-compatible model on Hugging Face
3. Restart service to download and load model

---

## 🧪 Testing

### Run Test Suite

```bash
# Local
python test_client.py

# Remote
python test_client.py https://your-service-url.com

# With debugging
DEBUG=True python test_client.py
```

### Test Coverage

- ✅ Health check endpoint
- ✅ Single target translation
- ✅ Multiple target translation (parallel)
- ✅ All 6 languages
- ✅ Unknown language handling
- ✅ Beam search parameter
- ✅ Custom max_decoding_length
- ✅ Input validation (empty text)
- ✅ Input validation (missing targets)

---

## 📦 Docker

The reference image is published on the GitHub Container Registry (CUDA 12, linux/amd64):

```bash
docker pull ghcr.io/bsc-appliedintelligence/traduia/modelskit:2026.11.26
docker run --rm --gpus all -p 8002:8000 -v modelskit_workspace:/workspace \
  ghcr.io/bsc-appliedintelligence/traduia/modelskit:2026.11.26
```

Models are downloaded from Hugging Face on first start into the `/workspace` volume (`HF_HOME`), so keep that volume.
The service then listens on `localhost:8002`.

### Build your own

`Dockerfile.gpu.ct2-source` is a two-stage build: CTranslate2 is compiled with CUDA in the `devel` image and only the
library, the Python environment and the app are copied into the `runtime` image (about 5 GB). Build from this directory:

```bash
docker build -f Dockerfile.gpu.ct2-source -t traduia-modelskit .
# or build and push with the helper (REGISTRY, IMAGE_NAME, PUSH and PLATFORMS can be overridden)
bash scripts/docker_build_and_push.sh
```

The first build compiles CTranslate2 and takes a while.

### Docker Compose

```yaml
services:
  translator:
    image: ghcr.io/bsc-appliedintelligence/traduia/modelskit:2026.11.26
    ports:
      - "8002:8000"
    volumes:
      - modelskit_workspace:/workspace
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    environment:
      - TRANSLATE_WORKERS=8
volumes:
  modelskit_workspace:
```

---

## 📊 Monitoring

### Health Check

Integrate with monitoring tools:

```bash
# Kubernetes liveness probe
livenessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 300
  periodSeconds: 30

# Docker healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=300s \
  CMD curl -f http://localhost:8000/health || exit 1
```

### Logging

Structured logging with levels:

```python
# Adjust log level
logging.basicConfig(level=logging.DEBUG)  # DEBUG, INFO, WARNING, ERROR
```

---

## 🤝 Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## 📄 License

The code is released under the Apache License 2.0 (see the repository `LICENSE`). It uses models from [Projecte AINA](https://projecte-aina.github.io/), also released under Apache 2.0.

---

## 🙏 Acknowledgments

- **Projecte AINA**: For training and releasing high-quality Catalan translation models
- **CTranslate2**: For the efficient inference engine
- **Hugging Face**: For model hosting and distribution
- **FastAPI**: For the excellent web framework

---

## 📞 Support

- **Issues**: [GitHub Issues](https://github.com/BSC-AppliedIntelligence/traduia/issues)
- **Projecte AINA**: [https://projecte-aina.github.io/](https://projecte-aina.github.io/)
