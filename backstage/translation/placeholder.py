"""
Placeholder translator - devuelve el texto original sin traducir.

Usar hasta que se integren los modelos Aino para traducción real.
"""
import logging
from .base import BaseTranslator

logger = logging.getLogger(__name__)


class NullTranslator(BaseTranslator):
    """
    Translator que devuelve el texto original sin cambios.

    Útil para:
    - Testing sin backend de traducción
    - Cuando source == target language
    - Placeholder hasta integrar Aino
    """

    def __init__(self, source_language: str = "auto", target_language: str = "es"):
        super().__init__(source_language, target_language)
        logger.info(f"📝 NullTranslator: {source_language} → {target_language} (passthrough)")

    async def translate(self, text: str) -> str:
        """Devuelve el texto sin traducir."""
        return text
