"""
Translation module for Link3rs Backstage.

Provides pluggable translation backends:
- NullTranslator: Placeholder (returns original text)
- AinaTranslator: AINA Translator API (ca → en/es/fr/pt/it/de)
"""
from .base import BaseTranslator
from .placeholder import NullTranslator
from .aina_translator import AinaTranslator

__all__ = ["BaseTranslator", "NullTranslator", "AinaTranslator"]
