"""
Buffer de text per TTS específic per CATALÀ amb OpenAI.

REGLES PER CATALÀ/OPENAI:
- Esperar SEMPRE al PUNT (frases completes)
- EXCEPCIÓ: Si >80 chars, pot tallar per coma
- Interrogants i exclamacions també tallen

Per què? OpenAI amb català sona malament si talla a mitja frase.
"""
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Configuració per català
MIN_CHARS_FOR_PUNCT = 10     # Mínim chars per sintetitzar (evitar "Sí.")
MAX_CHARS_BEFORE_COMMA = 80  # Màxim chars abans de forçar tall per coma


class TTSBufferCatalan:
    """
    Buffer intel·ligent per català amb OpenAI.
    
    REGLA PRINCIPAL: Esperar al PUNT
    EXCEPCIÓ: Si >80 chars, tallar per coma
    """
    
    def __init__(self, max_chars_before_comma: int = MAX_CHARS_BEFORE_COMMA):
        self._buffer = ""
        self._max_chars = max_chars_before_comma
    
    def add_chunk(self, text: str, punctuation: str) -> Optional[str]:
        """
        Afegeix text al buffer.
        
        Returns:
            Text a sintetitzar si és moment, None si cal esperar
        """
        text = text.strip()
        if not text:
            return None
        
        # Acumular
        if self._buffer:
            self._buffer += " " + text
        else:
            self._buffer = text
        
        buffer_len = len(self._buffer)
        
        # PUNT, INTERROGANT, EXCLAMACIÓ → SINTETITZAR (si >10 chars)
        if punctuation in '.?!。？！':
            if buffer_len >= MIN_CHARS_FOR_PUNCT:
                result = self._clean_and_return()
                logger.debug(f"[TTS-CA] Punt: '{result[:50]}...' ({len(result)} chars)")
                return result
            # Molt curt, esperar més
            return None
        
        # COMA → NOMÉS si >80 chars (frase llarga)
        if punctuation in ',;:':
            if buffer_len >= self._max_chars:
                result = self._clean_and_return()
                logger.debug(f"[TTS-CA] Coma llarga: '{result[:50]}...' ({len(result)} chars)")
                return result
            # Menys de 80 chars, esperar al punt
            return None
        
        # Sense puntuació → esperar
        return None
    
    def _clean_and_return(self) -> str:
        """Neteja el buffer i retorna el text."""
        text = self._buffer
        self._buffer = ""
        
        # Netejar espais
        text = re.sub(r'\s+', ' ', text).strip()
        
        # Eliminar repeticions curtes
        text = self._remove_short_repetitions(text)
        
        return text
    
    def _remove_short_repetitions(self, text: str) -> str:
        """Elimina repeticions curtes com 'sí, sí, sí'."""
        # Detectar "X, X, X" on X < 5 chars
        pattern = r'\b(\w{1,4})(,\s*\1)+\b'
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
        # Netejar comes soltes
        text = re.sub(r',\s*,', ',', text)
        text = re.sub(r'^\s*,\s*', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text
    
    def flush(self) -> Optional[str]:
        """Força síntesi del buffer (fi de stream)."""
        if self._buffer.strip():
            result = self._clean_and_return()
            return result if len(result) >= 3 else None
        return None
    
    def clear(self):
        """Neteja el buffer."""
        self._buffer = ""
    
    @property
    def current_length(self) -> int:
        return len(self._buffer)


class TTSBufferGeneric:
    """
    Buffer genèric per altres idiomes (es, en, fr, etc).
    
    REGLES:
    - Punt/interrogant/exclamació: sintetitzar si >15 chars
    - Coma: sintetitzar si >40 chars
    """
    
    def __init__(self, min_chars_punct: int = 15, min_chars_comma: int = 40):
        self._buffer = ""
        self._min_punct = min_chars_punct
        self._min_comma = min_chars_comma
    
    def add_chunk(self, text: str, punctuation: str) -> Optional[str]:
        text = text.strip()
        if not text:
            return None
        
        # Acumular
        if self._buffer:
            self._buffer += " " + text
        else:
            self._buffer = text
        
        buffer_len = len(self._buffer)
        
        # PUNT, INTERROGANT, EXCLAMACIÓ
        if punctuation in '.?!。？！':
            if buffer_len >= self._min_punct:
                result = self._clean_and_return()
                return result
            return None
        
        # COMA
        if punctuation in ',;:':
            if buffer_len >= self._min_comma:
                result = self._clean_and_return()
                return result
            return None
        
        return None
    
    def _clean_and_return(self) -> str:
        text = self._buffer
        self._buffer = ""
        text = re.sub(r'\s+', ' ', text).strip()
        return text
    
    def flush(self) -> Optional[str]:
        if self._buffer.strip():
            result = self._clean_and_return()
            return result if len(result) >= 3 else None
        return None
    
    def clear(self):
        self._buffer = ""
    
    @property
    def current_length(self) -> int:
        return len(self._buffer)


def create_tts_buffer(language: str):
    """
    Crea el buffer apropiat per l'idioma.
    
    - Català (ca): Esperar al punt (OpenAI)
    - Altres: Buffer genèric
    """
    if language == "ca":
        return TTSBufferCatalan()
    else:
        return TTSBufferGeneric()


# Test
if __name__ == "__main__":
    print("=== Test TTSBufferCatalan ===\n")
    
    buffer = TTSBufferCatalan()
    
    # Simular frases
    chunks = [
        ("Hola,", ","),
        ("com estàs?", "?"),  # → Sintetitzar aquí (interrogant)
        ("Jo estic bé,", ","),
        ("gràcies per preguntar.", "."),  # → Sintetitzar aquí (punt)
        ("Això és una frase molt llarga que passa dels vuitanta caràcters i per tant,", ","),  # → Sintetitzar (>80 chars coma)
        ("s'hauria de tallar.", "."),  # → Sintetitzar
    ]
    
    for text, punct in chunks:
        result = buffer.add_chunk(text, punct)
        if result:
            print(f"  ✅ SYNTH: '{result}'")
        else:
            print(f"  ⏳ Wait: '{text}' (buffer: {buffer.current_length} chars)")
