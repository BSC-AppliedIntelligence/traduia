"""
Interfaz abstracta para traductores.

Todos los backends de traducción deben implementar esta interfaz.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional
import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranslationResult:
    text: str
    status: str = "translated"
    error: str = ""


class BaseTranslator(ABC):
    """
    Interfaz base para traductores.

    Cada agente de traducción recibe una instancia de BaseTranslator
    para traducir los chunks de texto transcritos por Whisper Live.
    """

    def __init__(self, source_language: str = "auto", target_language: str = "es"):
        self.source_language = source_language
        self.target_language = target_language

    @abstractmethod
    async def translate(self, text: str) -> str:
        """
        Traduce un texto del idioma origen al idioma destino.

        Args:
            text: Texto a traducir

        Returns:
            Texto traducido
        """
        pass

    async def translate_with_status(self, text: str, *, chunk_id: str = "") -> TranslationResult:
        """Per-request metadata; never infer failure from identical wording."""
        return TranslationResult(await self.translate(text))

    async def translate_batch(self, texts: List[str]) -> List[str]:
        """
        Traduce múltiples textos. Por defecto, llama translate() para cada uno.
        Los backends pueden sobreescribir esto para optimizar batch.

        Args:
            texts: Lista de textos a traducir

        Returns:
            Lista de textos traducidos
        """
        results = []
        for text in texts:
            translated = await self.translate(text)
            results.append(translated)
        return results
