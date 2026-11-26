"""
AINA Translator Microservice
=============================
Real-time translation from Catalan to multiple languages using
Projecte AINA CTranslate2 models from Hugging Face.

Supported targets: en, es, fr, pt, it, de

These models use CTranslate2 + pyonmttok (SentencePiece), NOT HF Transformers.
"""

import os
import time
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Optional

import ctranslate2
import pyonmttok
from huggingface_hub import snapshot_download
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("aina-translator")

# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------
# Note: ca-de uses "spm.50k.model", the rest use "spm.model"
MODEL_REGISTRY: dict[str, dict] = {
    "en": {"repo": "projecte-aina/aina-translator-ca-en", "spm": "spm.model"},
    "es": {"repo": "projecte-aina/aina-translator-ca-es", "spm": "spm.model"},
    "fr": {"repo": "projecte-aina/aina-translator-ca-fr", "spm": "spm.model"},
    "pt": {"repo": "projecte-aina/aina-translator-ca-pt", "spm": "spm.model"},
    "it": {"repo": "projecte-aina/aina-translator-ca-it", "spm": "spm.model"},
    "de": {"repo": "projecte-aina/aina-translator-ca-de", "spm": "spm.50k.model"},
}

# Runtime stores (populated on startup)
tokenizers: dict[str, pyonmttok.Tokenizer] = {}
translators: dict[str, ctranslate2.Translator] = {}

# ---------------------------------------------------------------------------
# Device detection
# ---------------------------------------------------------------------------
HAS_GPU = ctranslate2.get_cuda_device_count() > 0
DEVICE = "cuda" if HAS_GPU else "cpu"

logger.info(f"Device: {DEVICE} | CUDA devices: {ctranslate2.get_cuda_device_count()}")

# Thread pool for parallel translations
MAX_WORKERS = int(os.environ.get("TRANSLATE_WORKERS", 6))
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def load_models() -> None:
    """Download and load all CTranslate2 translation models into memory."""
    logger.info("Starting model download & loading …")
    t0 = time.time()

    for lang, cfg in MODEL_REGISTRY.items():
        repo = cfg["repo"]
        spm_name = cfg["spm"]

        logger.info(f"  Downloading {lang} ← {repo}")
        t1 = time.time()

        # Download model files from HuggingFace
        model_dir = snapshot_download(repo_id=repo, revision="main")
        logger.info(f"  Downloaded to {model_dir}")

        # Load SentencePiece tokenizer
        spm_path = os.path.join(model_dir, spm_name)
        if not os.path.exists(spm_path):
            # Fallback: try to find any spm file
            for f in os.listdir(model_dir):
                if f.startswith("spm") and f.endswith(".model"):
                    spm_path = os.path.join(model_dir, f)
                    logger.warning(f"  ⚠ Expected {spm_name}, using {f} instead")
                    break

        tok = pyonmttok.Tokenizer(mode="none", sp_model_path=spm_path)

        # Load CTranslate2 translator
        translator = ctranslate2.Translator(
            model_dir,
            device=DEVICE,
            compute_type="float16" if HAS_GPU else "float32",
        )

        tokenizers[lang] = tok
        translators[lang] = translator

        logger.info(f"  ✓ {lang} loaded in {time.time() - t1:.1f}s")

    logger.info(f"All models loaded in {time.time() - t0:.1f}s")


# ---------------------------------------------------------------------------
# FastAPI lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_models()
    yield
    translators.clear()
    tokenizers.clear()


app = FastAPI(
    title="AINA Translator",
    description="Catalan → multi-language translation using Projecte AINA CTranslate2 models",
    version="2.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class TranslateRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Catalan source text")
    targets: list[str] = Field(
        ...,
        description="List of target language codes (en, es, fr, pt, it, de)",
    )
    max_decoding_length: int = Field(
        default=256,
        ge=1,
        le=1024,
        description="Maximum number of tokens to generate per translation",
    )
    beam_size: int = Field(
        default=1,
        ge=1,
        le=10,
        description="Beam search width (1 = greedy, higher = better quality but slower)",
    )


class TranslateResponse(BaseModel):
    """Keys are language codes; values are translated strings or null."""
    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Translation logic
# ---------------------------------------------------------------------------

def translate_single(
    text: str,
    lang: str,
    max_decoding_length: int = 256,
    beam_size: int = 1,
) -> Optional[str]:
    """Translate *text* from Catalan to *lang*. Returns None if lang unknown."""
    if lang not in translators:
        return None

    tok = tokenizers[lang]
    translator = translators[lang]

    # Tokenize with SentencePiece
    tokenized = tok.tokenize(text)
    tokens = tokenized[0]  # pyonmttok returns (tokens, features)

    # Translate
    results = translator.translate_batch(
        [tokens],
        beam_size=beam_size,
        max_decoding_length=max_decoding_length,
    )

    # Detokenize
    translation = tok.detokenize(results[0].hypotheses[0])
    return translation


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/translate", response_model=TranslateResponse)
async def translate(req: TranslateRequest):
    """Translate Catalan text to one or more target languages (in parallel)."""
    loop = asyncio.get_event_loop()

    # Launch all requested translations concurrently via the thread pool
    tasks: dict[str, asyncio.Future] = {}
    for lang in req.targets:
        lang_lower = lang.strip().lower()
        tasks[lang_lower] = loop.run_in_executor(
            executor,
            translate_single,
            req.text,
            lang_lower,
            req.max_decoding_length,
            req.beam_size,
        )

    # Await all results in parallel
    results: dict[str, Optional[str]] = {}
    for lang_lower, future in tasks.items():
        results[lang_lower] = await future

    return results


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "device": DEVICE,
        "models_loaded": list(translators.keys()),
    }


@app.get("/")
async def root():
    return {
        "service": "AINA Translator",
        "version": "2.0.0",
        "engine": "CTranslate2",
        "docs": "/docs",
        "health": "/health",
    }
