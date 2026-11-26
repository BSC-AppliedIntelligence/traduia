"""
AINA Translator — Cliente async para el servicio de traducción AINA.

Traduce texto catalán (ca) a uno de los idiomas soportados:
en, es, fr, pt, it, de

El servicio AINA es stateless HTTP (FastAPI) y ejecuta las traducciones
en paralelo internamente. Cada instancia de AinaTranslator traduce
a UN idioma target (una por canal/agent).

Documentación completa: AINA_TRANSLATOR_API_DOCS.md
"""
import logging
import time
import traceback
import httpx
from typing import Optional

from .base import BaseTranslator, TranslationResult

logger = logging.getLogger(__name__)

# Idiomas soportados por AINA Translator
SUPPORTED_TARGETS = {"en", "es", "fr", "pt", "it", "de"}


class AinaTranslator(BaseTranslator):
    """
    Traductor que usa el servicio AINA Translator (self-hosted en RunPod).

    Cada instancia traduce catalán → un idioma target específico.
    Usa httpx async para las llamadas HTTP.
    """

    def __init__(
        self,
        base_url: str,
        target_language: str,
        source_language: str = "ca",
        num_beams: int = 1,
        max_new_tokens: int = 256,
        timeout: float = 30.0,
    ):
        super().__init__(source_language=source_language, target_language=target_language)
        self.base_url = base_url.rstrip("/")
        self.translate_url = f"{self.base_url}/translate"
        self.health_url = f"{self.base_url}/health"
        self.num_beams = num_beams
        self.max_new_tokens = max_new_tokens
        self.timeout = timeout

        # Reusable async HTTP client (connection pooling)
        self._client: Optional[httpx.AsyncClient] = None

        if target_language not in SUPPORTED_TARGETS:
            logger.warning(
                f"⚠️ AinaTranslator: '{target_language}' no está en los idiomas soportados "
                f"({SUPPORTED_TARGETS}). La API devolverá null para este idioma."
            )

        logger.info(
            f"🌐 AinaTranslator: {source_language} → {target_language} | "
            f"URL: {self.base_url} | beams={num_beams}"
        )

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the async HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._client

    async def translate(self, text: str) -> str:
        """Compatibility API: return source text if translation fails."""
        return (await self.translate_with_status(text)).text

    async def translate_with_status(self, text: str, *, chunk_id: str = "") -> TranslationResult:
        if not text or not text.strip():
            return TranslationResult(text)

        started = time.monotonic()
        try:
            client = await self._get_client()

            payload = {
                "text": text,
                "targets": [self.target_language],
                "num_beams": self.num_beams,
                "max_new_tokens": self.max_new_tokens,
            }

            response = await client.post(self.translate_url, json=payload)
            response.raise_for_status()

            data = response.json()
            translated = data.get(self.target_language)

            if not isinstance(translated, str) or not translated.strip():
                logger.warning(
                    "AINA_TRANSLATION_FALLBACK chunk=%s target=%s reason=invalid_response chars_in=%d duration_ms=%.1f",
                    chunk_id or "unknown", self.target_language, len(text),
                    (time.monotonic() - started) * 1000,
                )
                return TranslationResult(text, "fallback", "invalid_response")

            logger.debug(
                f"🌐 [{self.target_language}] '{text[:40]}...' → '{translated[:40]}...'"
            )
            return TranslationResult(translated)

        except Exception as e:
            if isinstance(e, httpx.TimeoutException):
                reason = "timeout"
            elif isinstance(e, httpx.HTTPStatusError):
                reason = "http_error"
            elif isinstance(e, (ValueError, TypeError, AttributeError)):
                reason = "invalid_response"
            else:
                reason = "request_error"
            # Include type and stack even when str(e) is empty. Deliberately
            # omit exception strings, response bodies, URLs and local variables:
            # those can contain source text, credentials or service internals.
            logger.error(
                "AINA_TRANSLATION_FALLBACK chunk=%s target=%s reason=%s error_type=%s "
                "http_status=%s duration_ms=%.1f timeout_s=%s chars_in=%d\n%s",
                chunk_id or "unknown", self.target_language, reason, type(e).__name__,
                e.response.status_code if isinstance(e, httpx.HTTPStatusError) else "none",
                (time.monotonic() - started) * 1000, self.timeout, len(text),
                "".join(traceback.format_tb(e.__traceback__)),
            )
            return TranslationResult(text, "fallback", reason)

    async def health_check(self) -> dict:
        """
        Verifica que el servicio AINA está disponible y los modelos cargados.

        Returns:
            Dict con status, device, models_loaded
        """
        try:
            client = await self._get_client()
            response = await client.get(self.health_url, timeout=10.0)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"❌ AINA health check failed: {e}")
            return {"status": "error", "error": str(e)}

    async def close(self):
        """Cierra el cliente HTTP."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
