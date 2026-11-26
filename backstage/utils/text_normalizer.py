"""
Normalizador de texto para TTS.

Convierte números con formato europeo/americano a formato legible por TTS.
Ejemplos:
- "14.000" → "14000" (el TTS dirá "catorce mil")
- "1.500.000" → "1500000"
- "14,5" → "14.5" (decimal)
"""

import re
import logging

logger = logging.getLogger(__name__)


def normalize_numbers_for_tts(text: str, language: str = "es") -> str:
    """
    Normaliza números en el texto para que el TTS los pronuncie correctamente.
    
    Reglas:
    1. Números con punto como separador de miles (formato europeo): 14.000 → 14000
    2. Números con coma como decimal (formato europeo): 14,5 → 14.5
    3. Preservar puntuación normal de oraciones
    
    Args:
        text: Texto a normalizar
        language: Código de idioma (es, en, fr, de, it, pt)
    
    Returns:
        Texto normalizado
    """
    if not text:
        return text
    
    original = text
    
    # Patrón para números con separadores de miles (formato europeo)
    # Matches: 14.000, 1.500.000, 14.000.000
    # El punto debe estar seguido de exactamente 3 dígitos
    thousands_pattern = r'(\d{1,3})(?:\.(\d{3}))+(?!\d)'
    
    def replace_thousands(match):
        """Reemplaza puntos de miles: 14.000 → 14000"""
        full_match = match.group(0)
        # Remover todos los puntos
        return full_match.replace('.', '')
    
    text = re.sub(thousands_pattern, replace_thousands, text)
    
    # Patrón para decimales con coma (formato europeo)
    # Matches: 14,5 o 14,50 o 3,14159
    # La coma debe estar entre dígitos y NO seguida de 3 dígitos exactos (eso sería separador de miles americano)
    decimal_pattern = r'(\d+),(\d{1,2}(?!\d)|(?:\d{4,}))'
    
    def replace_decimal(match):
        """Reemplaza coma decimal: 14,5 → 14.5"""
        return f"{match.group(1)}.{match.group(2)}"
    
    text = re.sub(decimal_pattern, replace_decimal, text)
    
    # También manejar formato americano de miles (1,000,000) → 1000000
    american_thousands_pattern = r'(\d{1,3})(?:,(\d{3}))+(?!\d)'
    
    def replace_american_thousands(match):
        """Reemplaza comas de miles americanas: 1,000 → 1000"""
        full_match = match.group(0)
        return full_match.replace(',', '')
    
    text = re.sub(american_thousands_pattern, replace_american_thousands, text)
    
    if text != original:
        logger.debug(f"[TEXT-NORM] '{original}' → '{text}'")
    
    return text


def normalize_text_for_tts(text: str, language: str = "es") -> str:
    """
    Normalización completa de texto para TTS.
    
    Incluye:
    - Normalización de números
    - (Futuro: abreviaciones, símbolos, etc.)
    
    Args:
        text: Texto a normalizar
        language: Código de idioma
    
    Returns:
        Texto normalizado
    """
    text = normalize_numbers_for_tts(text, language)
    
    # Futuras normalizaciones pueden agregarse aquí:
    # - Abreviaciones (Dr. → Doctor)
    # - Símbolos (% → por ciento)
    # - URLs
    # - Fechas
    
    return text


# Tests básicos
if __name__ == "__main__":
    test_cases = [
        ("El avión vuela a 14.000 pies", "El avión vuela a 14000 pies"),
        ("Costó 1.500.000 euros", "Costó 1500000 euros"),
        ("La temperatura es 36,5 grados", "La temperatura es 36.5 grados"),
        ("Hay 1,000,000 de personas", "Hay 1000000 de personas"),
        ("Son las 14.30 horas", "Son las 14.30 horas"),  # No debería cambiar (no es miles)
        ("El precio es 99.99", "El precio es 99.99"),  # No debería cambiar (decimal americano)
        ("Velocidad: 1.200 km/h.", "Velocidad: 1200 km/h."),
    ]
    
    print("Testing number normalization:")
    for input_text, expected in test_cases:
        result = normalize_numbers_for_tts(input_text)
        status = "✓" if result == expected else "✗"
        print(f"{status} '{input_text}' → '{result}'")
        if result != expected:
            print(f"  Expected: '{expected}'")
