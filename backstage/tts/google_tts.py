"""
Cliente de Google Cloud Text-to-Speech con speaking_rate dinámico.

El speaking_rate se ajusta basándose en la velocidad del hablante original
para mantener sincronización sin artefactos de audio.

SPEAKING_RATE:
- 0.8 = 20% más lento
- 1.0 = velocidad normal
- 1.5 = 50% más rápido

Google TTS mantiene el pitch constante al cambiar speaking_rate,
lo que lo hace ideal para nuestro caso de uso.
"""
import asyncio
from typing import AsyncIterator, Optional, Tuple
import logging
import os

logger = logging.getLogger(__name__)

# Mapeo de idioma → código de idioma para Google TTS
LANGUAGE_CODES = {
    "en": "en-US",
    "es": "es-ES",
    "fr": "fr-FR",
    "de": "de-DE",
    "it": "it-IT",
    "pt": "pt-BR",
    "ca": "ca-ES",
}

# Voces Neural2 fallback (si no se usa Chirp)
NEURAL2_VOICES = {
    "en": "en-US-Neural2-J",
    "es": "es-ES-Neural2-A",
    "fr": "fr-FR-Neural2-A",
    "de": "de-DE-Neural2-A",
    "it": "it-IT-Neural2-A",
    "pt": "pt-BR-Neural2-A",
}

# Límites de speaking_rate (Google TTS soporta 0.25-4.0, pero calidad óptima en este rango)
MIN_SPEAKING_RATE = 0.8
MAX_SPEAKING_RATE = 1.5
DEFAULT_SPEAKING_RATE = 1.0


class GoogleTTS:
    """
    Cliente de Google Cloud TTS con speaking_rate dinámico.
    
    El speaking_rate se ajusta para coincidir con el ritmo del hablante original,
    manteniendo el pitch constante (sin efecto ardilla).
    """
    
    def __init__(
        self,
        credentials_path: Optional[str] = None,
        language: str = "en",
        voice: Optional[str] = None,
        sample_rate: int = 24000,
        default_speaking_rate: float = DEFAULT_SPEAKING_RATE,
    ):
        self.language = language
        self.language_code = LANGUAGE_CODES.get(language, f"{language}-{language.upper()}")
        self.sample_rate = sample_rate
        self.credentials_path = credentials_path

        # Read TTS config from environment
        tts_provider = os.environ.get("TTS_PROVIDER", "google_regular").strip()
        tts_voice_name = os.environ.get("TTS_VOICE", "").strip()
        self.use_chirp = tts_provider == "google_chirp"

        if voice:
            # Explicit voice passed — use it directly
            self.voice = voice
        elif self.use_chirp and tts_voice_name:
            # Chirp 3 HD: build voice name as {lang_code}-Chirp3-HD-{VoiceName}
            self.voice = f"{self.language_code}-Chirp3-HD-{tts_voice_name}"
        else:
            # Neural2 fallback
            self.voice = NEURAL2_VOICES.get(language, f"{self.language_code}-Neural2-A")

        self._client = None
        self._texttospeech = None
        self._initialized = False
        self._init_error = None

        # Speaking rate (ajustable dinámicamente)
        self._speaking_rate = default_speaking_rate

        logger.info(
            f"GoogleTTS initialized: lang={language}, voice={self.voice}, "
            f"provider={tts_provider}, chirp={'yes' if self.use_chirp else 'no'}"
        )
    
    @property
    def speaking_rate(self) -> float:
        """Speaking rate actual."""
        return self._speaking_rate
    
    @speaking_rate.setter
    def speaking_rate(self, value: float):
        """
        Establece el speaking rate.
        
        Args:
            value: Rate entre MIN_SPEAKING_RATE y MAX_SPEAKING_RATE
        """
        self._speaking_rate = max(MIN_SPEAKING_RATE, min(MAX_SPEAKING_RATE, value))
    
    def _ensure_client(self):
        """Inicializa el cliente de forma lazy."""
        if self._initialized:
            return
        
        self._initialized = True
        
        # Check credentials
        if self.credentials_path:
            if os.path.exists(self.credentials_path):
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self.credentials_path
                logger.info(f"Using Google credentials from: {self.credentials_path}")
            else:
                self._init_error = f"Credentials file not found: {self.credentials_path}"
                logger.error(self._init_error)
                return
        elif "GOOGLE_APPLICATION_CREDENTIALS" in os.environ:
            creds_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
            if os.path.exists(creds_path):
                logger.info(f"Using Google credentials from env: {creds_path}")
            else:
                self._init_error = f"Credentials file from env not found: {creds_path}"
                logger.error(self._init_error)
                return
        else:
            self._init_error = "No Google credentials configured"
            logger.warning(self._init_error)
            return
        
        try:
            from google.cloud import texttospeech
            self._texttospeech = texttospeech
            self._client = texttospeech.TextToSpeechClient()
            logger.info(f"Google TTS client ready: voice={self.voice}, lang={self.language_code}")
        except ImportError as e:
            self._init_error = f"google-cloud-texttospeech not installed: {e}"
            logger.error(self._init_error)
        except Exception as e:
            self._init_error = f"Failed to initialize Google TTS: {e}"
            logger.error(self._init_error)
    
    @property
    def is_available(self) -> bool:
        self._ensure_client()
        return self._client is not None
    
    async def synthesize(
        self,
        text: str,
        speaking_rate: Optional[float] = None,
    ) -> Tuple[bytes, float]:
        """
        Sintetiza texto a audio.
        
        Args:
            text: Texto a sintetizar
            speaking_rate: Rate opcional (usa self.speaking_rate si no se especifica)
            
        Returns:
            Tuple[bytes, float]: Audio PCM 16-bit y duración en segundos
        """
        if not text.strip():
            return b"", 0.0
        
        # Usar rate proporcionado o el actual
        rate = speaking_rate if speaking_rate is not None else self._speaking_rate
        rate = max(MIN_SPEAKING_RATE, min(MAX_SPEAKING_RATE, rate))
        
        self._ensure_client()
        
        if not self._client:
            logger.warning(f"TTS not available: {self._init_error}")
            return b"", 0.0
        
        try:
            loop = asyncio.get_event_loop()
            audio_content = await loop.run_in_executor(
                None, self._synthesize_sync, text, rate
            )
            
            if audio_content:
                # Calcular duración (16-bit PCM)
                duration = len(audio_content) / (self.sample_rate * 2)
                logger.info(f"[TTS] Synthesized: {len(text)} chars, {len(audio_content)} bytes, "
                           f"{duration:.2f}s, rate={rate:.2f}")
                return audio_content, duration
            
            return b"", 0.0
                    
        except Exception as e:
            logger.error(f"TTS synthesis error: {e}")
            return b"", 0.0
    
    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        """Sintetiza y devuelve audio."""
        audio, _ = await self.synthesize(text)
        if audio:
            yield audio
    
    def _synthesize_sync(self, text: str, speaking_rate: float) -> bytes:
        """Síntesis síncrona."""
        synthesis_input = self._texttospeech.SynthesisInput(text=text)
        
        voice = self._texttospeech.VoiceSelectionParams(
            language_code=self.language_code,
            name=self.voice,
        )
        
        audio_config = self._texttospeech.AudioConfig(
            audio_encoding=self._texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=self.sample_rate,
            speaking_rate=speaking_rate,  # Control de velocidad
        )
        
        try:
            response = self._client.synthesize_speech(
                input=synthesis_input,
                voice=voice,
                audio_config=audio_config,
            )
            return response.audio_content
        except Exception as e:
            logger.error(f"TTS API error: {e}")
            # Intentar con voz estándar
            try:
                voice = self._texttospeech.VoiceSelectionParams(
                    language_code=self.language_code,
                )
                response = self._client.synthesize_speech(
                    input=synthesis_input,
                    voice=voice,
                    audio_config=audio_config,
                )
                return response.audio_content
            except Exception as e2:
                logger.error(f"TTS fallback error: {e2}")
                return b""
