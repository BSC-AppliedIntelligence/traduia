"""
Link3rs Backstage - Real-Time Translation Platform
FastAPI + Supabase + LiveKit

Features:
- Event management with Supabase
- Real-time audio streaming with LiveKit
- Multi-language translation agents
- Speaker/Listener modes
- Real-time translation via AINA Translator
"""
import os
import sys
import json
import asyncio
import difflib
import logging
import re
import uuid
import time
import base64
import wave
import math
import numpy as np
from typing import Optional, Dict, List, Any, AsyncIterator
from dataclasses import dataclass, field
from collections import deque
from pathlib import Path
from math import gcd

from asr.caption_revisions import SourceCaptionRevisions
from asr.model_selection import ModelControlError
from asr.whisper_client import ASRStreamError
from translation.grouping import TranslationInput, group_prefix_translations, sentence_translation_inputs

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from pydantic import BaseModel
import uvicorn

from dotenv import load_dotenv


# Directory paths
BASE_DIR = Path(__file__).parent

# Load .env from the same directory as main.py (not CWD)
# This is critical for production where uvicorn may start from a different directory
load_dotenv(BASE_DIR / ".env")
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

# ============================================================================
# LOGGING
# ============================================================================

class ColoredFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[36m',
        'INFO': '\033[32m',
        'WARNING': '\033[33m',
        'ERROR': '\033[31m',
    }
    RESET = '\033[0m'
    
    def format(self, record):
        color = self.COLORS.get(record.levelname, self.RESET)
        record.levelname = f"{color}{record.levelname}{self.RESET}"
        record.msg = f"{color}{record.msg}{self.RESET}"
        return super().format(record)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s',
    datefmt='%H:%M:%S'
)

for handler in logging.root.handlers:
    handler.setFormatter(ColoredFormatter(
        '%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s',
        datefmt='%H:%M:%S'
    ))

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("grpc").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

logger = logging.getLogger("backstage")

# ============================================================================
# Configuration
# ============================================================================

def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning("Invalid float for %s=%r, using default %s", name, value, default)
        return default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("Invalid int for %s=%r, using default %s", name, value, default)
        return default


def _env_json_object(name: str) -> Optional[Dict[str, Any]]:
    value = os.getenv(name)
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON for %s=%r, ignoring", name, value)
        return None
    if not isinstance(parsed, dict):
        logger.warning("Invalid JSON for %s=%r, expected object, ignoring", name, value)
        return None
    return parsed


LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
# URL used only by server-side LiveKit API and room connections.  Keep the
# public URL separate when Backstage runs in Docker.
LIVEKIT_INTERNAL_URL = os.getenv("LIVEKIT_INTERNAL_URL", LIVEKIT_URL)
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
GOOGLE_CREDS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")

# Check LiveKit configuration
LIVEKIT_READY = bool(LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET)
TRANSLATION_READY = bool(GOOGLE_CREDS)  # Whisper Live no necesita API key (self-hosted)

# ============================================================================
# Supabase Client
# ============================================================================

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

# Application URL for redirects (Supabase email confirmation, etc.)
# In production, set this to your actual domain (e.g. https://yourdomain.com)
APP_URL = os.getenv("APP_URL", "http://localhost:8000")

# Whisper Live latency/runtime tuning
WHISPER_USE_VAD = _env_bool("WHISPER_USE_VAD", True)
WHISPER_NO_SPEECH_THRESH = _env_float("WHISPER_NO_SPEECH_THRESH", 0.45)
WHISPER_ACTIVE_TIMEOUT_S = _env_float("WHISPER_ACTIVE_TIMEOUT_S", 2.0)
WHISPER_CLIP_AUDIO = _env_bool("WHISPER_CLIP_AUDIO", False)
WHISPER_SAME_OUTPUT_THRESHOLD = _env_int("WHISPER_SAME_OUTPUT_THRESHOLD", 10)
WHISPER_VAD_PARAMETERS = _env_json_object("WHISPER_VAD_PARAMETERS")
WHISPER_MAX_UNCOMMITTED_AUDIO_S = _env_float("WHISPER_MAX_UNCOMMITTED_AUDIO_S", 0.0)
WHISPER_CONTEXT_OVERLAP_S = _env_float("WHISPER_CONTEXT_OVERLAP_S", 1.0)
WHISPER_TIMEOUT_REVISION_BUFFER_MS = _env_int("WHISPER_TIMEOUT_REVISION_BUFFER_MS", 500)
LATENCY_LOGGING = _env_bool("LATENCY_LOGGING", False)
# Only ES/EN targets of the negotiated BSC prefix protocol use this wait.
# Zero disables grouping for a controlled comparison; normal segments bypass it.
BSC_TRANSLATION_MAX_WAIT_S = _env_float("BSC_TRANSLATION_MAX_WAIT_S", 2.0)
if not math.isfinite(BSC_TRANSLATION_MAX_WAIT_S) or not 0 <= BSC_TRANSLATION_MAX_WAIT_S <= 2:
    logger.warning("BSC_TRANSLATION_MAX_WAIT_S must be between 0 and 2; using 2.0")
    BSC_TRANSLATION_MAX_WAIT_S = 2.0
SHARED_ASR_ENABLED = _env_bool("SHARED_ASR_ENABLED", True)
EXPERIMENT_ID = os.getenv("EXPERIMENT_ID", "").strip()
EXPERIMENT_RUN_ID = os.getenv("EXPERIMENT_RUN_ID", "").strip()
EXPERIMENT_REPETITION = os.getenv("EXPERIMENT_REPETITION", "").strip()
BACKSTAGE_RECORDINGS_DIR = Path(os.getenv("BACKSTAGE_RECORDINGS_DIR", "/app/recordings"))

logger.info(
    "Whisper runtime tuning: use_vad=%s no_speech_thresh=%s active_timeout_s=%s "
    "clip_audio=%s same_output_threshold=%s vad_parameters=%s "
    "max_uncommitted_audio_s=%s context_overlap_s=%s "
    "timeout_revision_buffer_ms=%s latency_logging=%s shared_asr_enabled=%s "
    "experiment_id=%s experiment_run_id=%s experiment_repetition=%s",
    WHISPER_USE_VAD,
    WHISPER_NO_SPEECH_THRESH,
    WHISPER_ACTIVE_TIMEOUT_S,
    WHISPER_CLIP_AUDIO,
    WHISPER_SAME_OUTPUT_THRESHOLD,
    WHISPER_VAD_PARAMETERS,
    WHISPER_MAX_UNCOMMITTED_AUDIO_S,
    WHISPER_CONTEXT_OVERLAP_S,
    WHISPER_TIMEOUT_REVISION_BUFFER_MS,
    LATENCY_LOGGING,
    SHARED_ASR_ENABLED,
    EXPERIMENT_ID or "none",
    EXPERIMENT_RUN_ID or "none",
    EXPERIMENT_REPETITION or "none",
)

supabase = None
_supabase_key_type = "none"

if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    try:
        from supabase import create_client
        supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        # Decode JWT payload to check role (base64 hides the literal string)
        try:
            import base64
            payload = SUPABASE_SERVICE_KEY.split(".")[1]
            payload += "=" * (4 - len(payload) % 4)  # fix padding
            decoded = json.loads(base64.urlsafe_b64decode(payload))
            _supabase_key_type = decoded.get("role", "unknown")
        except Exception:
            _supabase_key_type = "unknown"
        logger.info(f"✅ Supabase client initialized (key type: {_supabase_key_type})")
    except ImportError:
        logger.warning("⚠️ supabase-py not installed, some features disabled")
    except Exception as e:
        logger.warning(f"⚠️ Could not initialize Supabase: {e}")
else:
    logger.warning(f"⚠️ SUPABASE_URL or SUPABASE_SERVICE_KEY not configured")
    logger.warning(f"   SUPABASE_URL set: {bool(SUPABASE_URL)}")
    logger.warning(f"   SUPABASE_SERVICE_KEY set: {bool(SUPABASE_SERVICE_KEY)}")
    logger.warning(f"   .env path: {BASE_DIR / '.env'}")
    logger.warning(f"   .env exists: {(BASE_DIR / '.env').exists()}")

logger.info("=" * 60)
logger.info("Link3rs Backstage Server v3")
logger.info("=" * 60)
logger.info(f"LIVEKIT: {'✅' if LIVEKIT_READY else '❌ Not configured'}")
logger.info(f"WHISPER LIVE: self-hosted (no API key needed)")
logger.info(f"AINA TRANSLATOR: configured per event (Endpoints tab)")
logger.info(f"GOOGLE TTS: {'✅' if GOOGLE_CREDS else '❌'}")
logger.info(f"SUPABASE: {'✅' if supabase else '❌ Not configured'}")
logger.info("=" * 60)

# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(title="Link3rs Backstage v3")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Disable Jinja cache to avoid unhashable type errors with request objects
class NoCache:
    def get(self, key):
        return None
    def __setitem__(self, key, value):
        pass

templates.env.cache = NoCache()

# Global template context: inject Supabase config so frontend JS can use it
# This avoids hardcoding credentials in static JS files
# Note: Passing in context instead of globals to avoid Jinja caching issues
# templates.env.globals["SUPABASE_URL"] = SUPABASE_URL
# templates.env.globals["SUPABASE_ANON_KEY"] = SUPABASE_ANON_KEY
# templates.env.globals["APP_URL"] = APP_URL

# ============================================================================
# State Management
# ============================================================================

@dataclass
class TranslationSession:
    """
    Active translation session for an event.
    
    NEW ARCHITECTURE:
    - Session owns ALL tracks (created once, never deleted)
    - Session connects to LiveKit as "session-server"
    - Agents only handle Whisper Live ASR (no LiveKit connection)
    - When source == target, that channel plays original audio
    """
    event_id: str
    room_name: str
    source_language: Optional[str] = None
    target_languages: List[str] = field(default_factory=list)
    broadcaster_identity: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    is_active: bool = False
    
    # LiveKit components (owned by session, not agents)
    room: Optional[Any] = None
    audio_sources: Dict[str, Any] = field(default_factory=dict)  # lang -> AudioSource
    audio_publishers: Dict[str, Any] = field(default_factory=dict)  # lang -> AudioPublisher
    
    # Whisper agents (translation/TTS workers) plus one shared ASR owner.
    whisper_agents: Dict[str, Any] = field(default_factory=dict)  # lang -> WhisperAgent
    source_asr_agent: Optional[Any] = None  # Single shared ASR owner for source audio

    # Whisper Live + AINA Translator configuration
    whisper_config: dict = field(default_factory=lambda: {"host": "localhost", "port": 9090, "model": "small", "language": "ca", "translator_url": ""})
    prepared_whisper: Optional[Any] = None
    asr_service_id: Optional[str] = None
    
    # Broadcaster tracking
    broadcaster_track: Optional[Any] = None
    audio_processor_task: Optional[asyncio.Task] = None


# Active sessions by event_id
active_sessions: Dict[str, TranslationSession] = {}
session_start_locks: Dict[str, asyncio.Lock] = {}

# Mapping event_id -> event_code for WebSocket broadcasts
event_id_to_code: Dict[str, str] = {}

# WebSocket connections for logs
log_connections: Dict[str, List[WebSocket]] = {}


# ============================================================================
# Captions WebSocket Manager (like FastAPI example)
# ============================================================================

class CaptionsConnectionManager:
    """Manages WebSocket connections for captions by event_code and channel."""
    
    def __init__(self):
        # Structure: {event_code: {channel: [websocket, ...]}}
        self.connections: Dict[str, Dict[str, List[WebSocket]]] = {}
    
    async def connect(self, websocket: WebSocket, event_code: str, channel: str):
        await websocket.accept()
        if event_code not in self.connections:
            self.connections[event_code] = {}
        if channel not in self.connections[event_code]:
            self.connections[event_code][channel] = []
        self.connections[event_code][channel].append(websocket)
        logger.info(f"📺 Captions client connected: {event_code}/{channel} (total: {len(self.connections[event_code][channel])})")
    
    def disconnect(self, websocket: WebSocket, event_code: str, channel: str):
        if event_code in self.connections and channel in self.connections[event_code]:
            if websocket in self.connections[event_code][channel]:
                self.connections[event_code][channel].remove(websocket)
                logger.info(f"📺 Captions client disconnected: {event_code}/{channel}")
    
    async def broadcast(self, event_code: str, channel: str, message: str):
        """Broadcast message to all clients subscribed to this event/channel."""
        if event_code in self.connections and channel in self.connections[event_code]:
            disconnected = []
            for websocket in self.connections[event_code][channel]:
                try:
                    await websocket.send_text(message)
                except Exception:
                    disconnected.append(websocket)
            # Clean up disconnected
            for ws in disconnected:
                self.connections[event_code][channel].remove(ws)
    
    async def broadcast_to_all_channels(self, event_code: str, message: str):
        """Broadcast to all channels of an event."""
        if event_code in self.connections:
            for channel in self.connections[event_code]:
                await self.broadcast(event_code, channel, message)
    
    def get_channels(self, event_code: str) -> List[str]:
        """Get list of channels with active connections for an event."""
        if event_code in self.connections:
            return list(self.connections[event_code].keys())
        return []


# Global captions manager
captions_manager = CaptionsConnectionManager()


# ============================================================================
# Models
# ============================================================================

class CreateSessionRequest(BaseModel):
    event_id: str
    event_title: str
    event_code: str = ""  # Event code for WebSocket broadcasts
    channels: List[dict]  # [{language: "es", language_code: "es-ES", is_original: bool}]
    source_language: str = "any"  # 'any' = auto-detect, or specific like 'en', 'es', etc.


class CreateSessionResponse(BaseModel):
    room_name: str
    livekit_url: str
    broadcaster_token: str
    session_id: str
    active_agents: List[str] = []  # Idiomas activos
    success: bool


class SourceLanguageRequest(BaseModel):
    source_language: str  # 'any' or specific language code


class JoinSessionRequest(BaseModel):
    identity: str
    role: str = "listener"
    language: Optional[str] = None


class JoinSessionResponse(BaseModel):
    token: str
    livekit_url: str
    room_name: str


# ============================================================================
# LiveKit Helpers
# ============================================================================

def sanitize_room_name(title: str) -> str:
    """Convert event title to valid room name."""
    import re
    # Replace spaces with hyphens, remove special chars
    name = re.sub(r'[^a-zA-Z0-9\s-]', '', title)
    name = re.sub(r'\s+', '-', name).strip('-')
    return name.lower()[:50] or f"room-{uuid.uuid4().hex[:8]}"


async def get_livekit_api():
    """Get LiveKit API client."""
    if not LIVEKIT_READY:
        raise HTTPException(500, "LiveKit not configured")
    
    from livekit import api
    api_url = LIVEKIT_INTERNAL_URL.replace("wss://", "https://").replace("ws://", "http://")
    return api.LiveKitAPI(api_url, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)


def create_token(
    room_name: str,
    identity: str,
    name: str = None,
    can_publish: bool = False,
    can_subscribe: bool = True,
) -> str:
    """Create LiveKit access token."""
    from livekit import api
    
    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(name or identity)
        .with_grants(api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=can_publish,
            can_publish_data=can_publish,
            can_subscribe=can_subscribe,
        ))
        .to_jwt()
    )
    return token


async def ensure_room_exists(room_name: str) -> bool:
    """Ensure LiveKit room exists, create if not."""
    if not LIVEKIT_READY:
        return False
    
    from livekit import api
    lkapi = await get_livekit_api()
    
    try:
        # Try to list rooms to see if it exists
        rooms = await lkapi.room.list_rooms(api.ListRoomsRequest())
        room_exists = any(r.name == room_name for r in rooms.rooms)
        
        if not room_exists:
            # Create room
            await lkapi.room.create_room(
                api.CreateRoomRequest(
                    name=room_name,
                    empty_timeout=600,
                    max_participants=100,
                )
            )
            logger.info(f"✅ Created LiveKit room: {room_name}")
        else:
            logger.info(f"📍 Room already exists: {room_name}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Error with room {room_name}: {e}")
        return False
    finally:
        await lkapi.aclose()


# ============================================================================
# Audio Processing - NATIVE LIVEKIT RESAMPLER VERSION
# ============================================================================

# ARCHITECTURE:
# 1. Use LiveKit's native rtc.AudioResampler (Sox-based, maintains state)
# 2. AudioSource at 48kHz (LiveKit standard)
# 3. For Whisper Live: Resample 48kHz → 16kHz using native resampler
# 4. For TTS: Resample 24kHz → 48kHz using native resampler + smooth fade
# 5. For passthrough: Send frames DIRECTLY (no processing)
#
# KEY FIX: Native resampler maintains internal state between frames,
# preventing discontinuities that cause robotic sound.


def apply_smooth_fade(samples: np.ndarray, fade_samples: int = 480) -> np.ndarray:
    """
    Apply very smooth fade in/out to avoid pops.
    
    Uses sin² curve for fade in, cos² curve for fade out.
    10ms fade at 48kHz (480 samples) - smooth and prevents clicks.
    """
    if len(samples) < fade_samples * 2:
        return samples
    
    audio = samples.astype(np.float32)
    
    # Sin² fade in (very smooth start)
    fade_in = np.sin(np.linspace(0, np.pi/2, fade_samples)) ** 2
    audio[:fade_samples] *= fade_in
    
    # Cos² fade out (very smooth end)
    fade_out = np.cos(np.linspace(0, np.pi/2, fade_samples)) ** 2
    audio[-fade_samples:] *= fade_out
    
    return np.clip(audio, -32768, 32767).astype(np.int16)


class AudioPublisher:
    """
    Audio publisher amb COLA ORDENADA per números de seqüència.
    
    CARACTERÍSTIQUES:
    - Àudios s'envien EN ORDRE encara que TTS acabi desordenat
    - Fade-in 500ms OPCIONAL (només per Google Chirp3)
    - Lock per evitar accés concurrent
    """
    
    SAMPLE_RATE = 48000  # LiveKit standard
    FRAME_MS = 20        # 20ms frames (WebRTC)
    FADE_IN_MS = 500     # 500ms fade-in (NOMÉS per Chirp3)
    
    def __init__(self, audio_source, tts_sample_rate: int = 24000):
        from livekit import rtc
        import asyncio
        
        self.audio_source = audio_source
        self.tts_sample_rate = tts_sample_rate
        self.is_running = False
        self.logger = logging.getLogger("audio_pub")
        
        # Càlculs
        self.frame_samples = int(self.SAMPLE_RATE * self.FRAME_MS / 1000)      # 960 samples
        self.fade_in_samples = int(self.SAMPLE_RATE * self.FADE_IN_MS / 1000)  # 24000 samples (500ms)
        
        # Native resampler
        self._tts_resampler = rtc.AudioResampler(
            input_rate=tts_sample_rate,
            output_rate=self.SAMPLE_RATE,
            num_channels=1,
            quality=rtc.AudioResamplerQuality.VERY_HIGH,
        )
        
        # Precalcular corba de fade-in (sin² per transició suau)
        self._fade_in = np.sin(np.linspace(0, np.pi/2, self.fade_in_samples)) ** 2
        
        # COLA ORDENADA per seqüència
        self._pending: dict = {}  # seq_num -> (audio_bytes, apply_fade)
        self._next_seq_to_send = 0
        self._lock = asyncio.Lock()
    
    async def start(self):
        self.is_running = True
        self._pending = {}
        self._next_seq_to_send = 0
        self.logger.info(f"AudioPublisher started (TTS: {self.tts_sample_rate}Hz → {self.SAMPLE_RATE}Hz, ordered queue)")
    
    async def stop(self):
        self.is_running = False
        self._pending = {}
        try:
            self.audio_source.clear_queue()
        except:
            pass
        self.logger.info("AudioPublisher stopped")
    
    def reset_sequence(self):
        """Reset sequence counter (cridar quan comença nova sessió)."""
        self._next_seq_to_send = 0
        self._pending = {}
    
    async def queue_audio_ordered(self, audio_bytes: bytes, sequence_num: int, apply_fade: bool = False):
        """
        Encola àudio amb número de seqüència per mantenir ordre.
        
        Args:
            audio_bytes: Àudio PCM 16-bit
            sequence_num: Número de seqüència (ordre d'arribada)
            apply_fade: Si True, aplica fade-in 500ms (per Chirp3)
        """
        if not self.is_running or len(audio_bytes) < 2:
            return
        
        async with self._lock:
            # Guardar a pending
            self._pending[sequence_num] = (audio_bytes, apply_fade)
            
            # Intentar enviar tots els pendents en ordre
            await self._send_pending_in_order()
    
    async def _send_pending_in_order(self):
        """Envia àudios pendents en ordre de seqüència."""
        while self._next_seq_to_send in self._pending:
            audio_bytes, apply_fade = self._pending.pop(self._next_seq_to_send)
            
            await self._send_audio(audio_bytes, apply_fade)
            
            self._next_seq_to_send += 1
        
        # Log si hi ha àudios esperant (gaps en seqüència)
        if self._pending:
            waiting = sorted(self._pending.keys())
            self.logger.debug(f"⏳ Waiting for seq {self._next_seq_to_send}, have: {waiting[:5]}")
    
    async def queue_audio(self, audio_bytes: bytes, apply_fade: bool = False):
        """
        Encola àudio SENSE ordre (compatibilitat).
        S'envia immediatament.
        """
        if not self.is_running or len(audio_bytes) < 2:
            return
        
        async with self._lock:
            await self._send_audio(audio_bytes, apply_fade)
    
    async def _send_audio(self, audio_bytes: bytes, apply_fade: bool = False):
        """
        Processa i envia UN àudio complet.
        
        Args:
            apply_fade: Si True, aplica fade-in 500ms (per Google Chirp3)
        """
        from livekit import rtc
        
        try:
            # 1. Convertir a samples
            samples_24k = np.frombuffer(audio_bytes, dtype=np.int16).copy()
            
            if len(samples_24k) < 100:  # Massa curt
                return
            
            # 2. Resamplear a 48kHz
            input_frame = rtc.AudioFrame(
                data=samples_24k.tobytes(),
                sample_rate=self.tts_sample_rate,
                num_channels=1,
                samples_per_channel=len(samples_24k),
            )
            
            resampled_frames = self._tts_resampler.push(input_frame)
            
            # Combinar frames
            all_samples = []
            for frame in resampled_frames:
                frame_samples = np.frombuffer(bytes(frame.data), dtype=np.int16)
                all_samples.append(frame_samples)
            
            if not all_samples:
                return
            
            samples = np.concatenate(all_samples).astype(np.float32)
            
            # 3. Aplicar FADE-IN només si apply_fade=True (Google Chirp3)
            if apply_fade and len(samples) >= self.fade_in_samples:
                samples[:self.fade_in_samples] *= self._fade_in
                self.logger.debug(f"🎚️ Applied 500ms fade-in")
            
            # 4. Enviar en frames de 20ms
            offset = 0
            total = len(samples)
            frames_sent = 0
            
            while offset < total:
                remaining = total - offset
                chunk_size = min(self.frame_samples, remaining)
                
                chunk = samples[offset:offset + chunk_size]
                
                # Padding si l'últim frame és curt
                if len(chunk) < self.frame_samples:
                    padding = np.zeros(self.frame_samples - len(chunk), dtype=np.float32)
                    chunk = np.concatenate([chunk, padding])
                
                chunk_int16 = np.clip(chunk, -32768, 32767).astype(np.int16)
                
                output_frame = rtc.AudioFrame(
                    data=chunk_int16.tobytes(),
                    sample_rate=self.SAMPLE_RATE,
                    num_channels=1,
                    samples_per_channel=self.frame_samples,
                )
                
                await self.audio_source.capture_frame(output_frame)
                frames_sent += 1
                offset += self.frame_samples
            
            duration_ms = (total / self.SAMPLE_RATE) * 1000
            self.logger.debug(f"🔊 Sent {frames_sent} frames ({duration_ms:.0f}ms)")
            
        except Exception as e:
            self.logger.error(f"Send audio error: {e}")
            import traceback
            traceback.print_exc()
    
    async def queue_passthrough_audio(self, frame):
        """Envia frame passthrough directament."""
        if not self.is_running:
            return
        
        async with self._lock:
            try:
                await self.audio_source.capture_frame(frame)
            except Exception as e:
                self.logger.error(f"Passthrough error: {e}")


# ============================================================================
# Pace Meter for Dynamic Speed Adjustment
# ============================================================================

class PaceMeter:
    """
    Mide la velocidad del orador y recomienda speaking rate para TTS.
    
    CONFIGURACIÓ PER TEMPS REAL:
    - min_rate: 1.20 (sempre accelerat per mantenir-se al dia)
    - max_rate: 1.36 (màxim abans de sonar artificial)
    - window_seconds: 15s per suavitzar variacions
    
    OpenAI TTS és més lent que Chirp3, per tant necessita velocitat més alta.
    """
    
    def __init__(
        self,
        window_seconds: float = 15.0,
        reference_pace: float = 17.3,
        min_rate: float = 1.20,  # Mínim accelerat per keep up
        max_rate: float = 1.36,  # Màxim natural
    ):
        self.window_seconds = window_seconds
        self.reference_pace = reference_pace
        self.min_rate = min_rate
        self.max_rate = max_rate
        
        self._chunks = []  # [(timestamp, char_count)]
        self._smoothed_pace = None
        self._smoothing_up = 0.3    # Rápido para subir
        self._smoothing_down = 0.15  # Lento para bajar
        self._backlog_boost = 0.0
    
    def add_chunk(self, text: str):
        """Añade un chunk para medir pace."""
        now = time.time()
        char_count = len(text.strip())
        
        self._chunks.append((now, char_count))
        
        # Limpiar chunks fuera de la ventana
        cutoff = now - self.window_seconds
        self._chunks = [(t, c) for t, c in self._chunks if t > cutoff]
    
    def set_backlog_boost(self, pending_items: int):
        """Ajusta boost según backlog de audio pendiente."""
        # +0.05 por cada 5 items pendientes
        self._backlog_boost = min(0.15, pending_items * 0.01)
    
    def get_metrics(self) -> dict:
        """Obtiene métricas actuales."""
        if len(self._chunks) < 2:
            return {
                "current_pace": self.reference_pace,
                "recommended_rate": 1.0 + self._backlog_boost,
                "sample_count": len(self._chunks),
            }
        
        # Calcular pace actual
        times = [t for t, _ in self._chunks]
        chars = [c for _, c in self._chunks]
        
        time_span = max(times) - min(times)
        total_chars = sum(chars)
        
        if time_span < 0.5:
            return {
                "current_pace": self.reference_pace,
                "recommended_rate": 1.0 + self._backlog_boost,
                "sample_count": len(self._chunks),
            }
        
        current_pace = total_chars / time_span
        
        # Suavizar
        if self._smoothed_pace is None:
            self._smoothed_pace = current_pace
        else:
            if current_pace > self._smoothed_pace:
                smoothing = self._smoothing_up
            else:
                smoothing = self._smoothing_down
            
            self._smoothed_pace = (
                smoothing * current_pace +
                (1 - smoothing) * self._smoothed_pace
            )
        
        # Calcular rate recomendado
        recommended_rate = self._smoothed_pace / self.reference_pace
        recommended_rate += self._backlog_boost
        recommended_rate = max(self.min_rate, min(self.max_rate, recommended_rate))
        
        return {
            "current_pace": round(self._smoothed_pace, 1),
            "recommended_rate": round(recommended_rate, 2),
            "sample_count": len(self._chunks),
        }
    
    def get_speaking_rate(self) -> float:
        """Obtiene el speaking rate recomendado."""
        metrics = self.get_metrics()
        return metrics["recommended_rate"]
    
    def reset(self):
        """Resetea el medidor."""
        self._chunks.clear()
        self._smoothed_pace = None


# ============================================================================
# Whisper Agent (ASR/TTS only - NO LiveKit connection)
# ============================================================================

# Constants for subtitle buffer
MAX_SUBTITLE_WORDS = 100  # Max words before removing oldest block

# LiveKit Data Channel Topics
TOPIC_SUBTITLE_PREFIX = "subtitle"      # Topic: subtitle.{lang} or subtitle.original


class WhisperAgent:
    """
    Agent that handles Whisper Live ASR, Translation, and TTS.

    CRITICAL: This agent does NOT connect to LiveKit.
    It receives audio from the session and sends TTS to the session's AudioPublisher.
    It uses the session's room to publish subtitles and translations via data channel.

    The session owns the tracks, not the agent.

    NEW ARCHITECTURE (Whisper Live + Translation):
    - Whisper Live provides ONLY transcription
    - Translation is handled by a separate translator module
    - Flow: Audio → Whisper Live → transcription → Translator → subtitles + TTS

    Subtitle channels:
    - is_primary_agent=True: sends original transcriptions to "original" channel
    - All agents: send translations to their target_language channel

    Subtitle flow:
    - Partial tokens are sent as growing text (for real-time display)
    - Final chunks are confirmed, then translated, then sent
    """

    def __init__(
        self,
        target_language: str,
        event_id: str,
        audio_publisher: 'AudioPublisher',
        room,  # Session's LiveKit room for publishing subtitles
        whisper_host: str = "localhost",
        whisper_port: int = 9090,
        whisper_model: str = "small",
        whisper_language: str = "ca",
        source_language: str = "any",
        is_primary_agent: bool = False,  # Only primary sends original transcriptions
        translator=None,  # BaseTranslator instance
        owns_whisper: bool = True,
    ):
        self.target_language = target_language
        self.event_id = event_id
        self.audio_publisher = audio_publisher  # Owned by session
        self.room = room  # Session's room for data channel
        self.whisper_host = whisper_host
        self.whisper_port = whisper_port
        self.whisper_model = whisper_model
        self.whisper_language = whisper_language
        self.source_language = source_language
        self.is_primary_agent = is_primary_agent  # Only this agent sends original transcriptions
        self.translator = translator  # BaseTranslator for translation
        self.owns_whisper = owns_whisper
        self.fanout_agents: list[Any] = []
        self.logger = logging.getLogger(f"whisper-{target_language}")

        # Whisper Live client or shared-source chunk consumer
        self.whisper = None
        self._asr_model_id = None
        self.is_running = False
        self.is_active = True  # False when in passthrough mode
        self._source_chunk_queue: Optional[asyncio.Queue] = None
        self._source_chunk_task: Optional[asyncio.Task] = None
        self._processed_chunk_count = 0
        self._tts_count = 0
        
        # TTS
        self.tts = None
        self._tts_uses_chirp = False
        
        # Task management
        self._transcription_task = None
        self._lock = asyncio.Lock()
        
        # TTS concurrency - PARAL·LEL amb ORDRE
        self._tts_pending = 0  # Comptador de TTS pendents (per acceleració)
        self._tts_tasks: list = []  # Tasques TTS actives
        self._tts_sequence = 0  # Comptador de seqüència per mantenir ordre
        
        # TTS buffer - genèric per tots els idiomes target
        from utils.tts_buffer_catalan import TTSBufferGeneric
        self._tts_text_buffer = TTSBufferGeneric(min_chars_punct=15, min_chars_comma=40)
        self.logger.info(f"📝 TTS Buffer: Genèric (punt 15, coma 40)")
        
        self._pace_meter = PaceMeter(window_seconds=15.0, reference_pace=17.3, min_rate=1.20, max_rate=1.36)
        
        # ============================================================
        # SUBTITLE BUFFERS - for accumulating partial tokens
        # ============================================================
        # Original transcription buffer (what the speaker is saying)
        self._original_partial_buffer = ""
        # Confirmed original text (finalized sentences)
        self._original_confirmed = []
        
        # Translation buffer (translated text)
        self._translation_partial_buffer = ""
        # Confirmed translation text
        self._translation_confirmed = []
        
        # Max confirmed sentences to keep in memory
        self._max_confirmed_sentences = 10

        # Timeout and bounded-window results can still revise retained context.
        self._caption_revisions = SourceCaptionRevisions()
        self._timeout_revision_generation = 0
        self._timeout_revision_active = False
        self._timeout_revision_chunk = None
        self._caption_drafts = {}
        self._caption_sequence = 0
        self._caption_stream_id = str(uuid.uuid4())
        self._timeout_revision_buffer_s = max(0.0, WHISPER_TIMEOUT_REVISION_BUFFER_MS / 1000.0)
        self._translation_max_wait_s = BSC_TRANSLATION_MAX_WAIT_S
        self._translation_sentence_context = _env_bool("BSC_TRANSLATION_SENTENCE_CONTEXT", True)
        self.asr_error = None

    async def start(self):
        """Initialize TTS and prepare for receiving audio."""
        self.is_running = True
        self.asr_error = None
        self._caption_revisions = SourceCaptionRevisions()
        self._timeout_revision_generation = 0
        self._timeout_revision_active = False
        self._timeout_revision_chunk = None
        self._caption_drafts.clear()
        self._caption_sequence = 0
        self._caption_stream_id = str(uuid.uuid4())
        self._processed_chunk_count = 0
        self._tts_count = 0

        # =========================================================================
        # TTS SELECTION — Google TTS per tots els idiomes target
        # Skip TTS if no audio_publisher (original-only agent)
        # =========================================================================
        if self.audio_publisher:
            from tts.google_tts import GoogleTTS
            self.tts = GoogleTTS(language=self.target_language, sample_rate=24000)
            self._tts_uses_chirp = self.tts.use_chirp
            self.logger.info(f"✅ TTS: {self.tts.voice} ({'Chirp3' if self._tts_uses_chirp else 'Neural2'})")
        else:
            self.tts = None
            self.logger.info(f"📝 No TTS (original-only transcription agent)")

        # Reset sequence counter
        self._tts_sequence = 0

        self.logger.info(f"🚀 WhisperAgent started for {self.target_language}")

    async def connect_whisper(self):
        """Connect to Whisper Live ASR."""
        if not self.owns_whisper:
            self.logger.info(f"🔁 Agent {self.target_language} consumes shared ASR chunks, not connecting Whisper")
            return
        if not self.is_active:
            self.logger.info(f"⏸️ Agent {self.target_language} is in passthrough mode, not connecting Whisper")
            return

        self.logger.info(f"🔗 Connecting Whisper Live: {self.whisper_host}:{self.whisper_port} → {self.target_language}")

        try:
            # SIEMPRE forzar catalán (ca) como idioma de transcripción.
            # Whisper Live solo transcribe — la traducción la hace AINA Translator.
            forced_language = "ca"
            self.logger.info(f"🔤 Whisper language forced to '{forced_language}' (config was '{self.whisper_language}')")

            # A fresh ASR connection restarts audio timestamps at zero.
            self._caption_revisions = SourceCaptionRevisions()
            if self.whisper is None:
                await ensure_shared_model_admission({"host": self.whisper_host, "port": self.whisper_port})
                self.whisper = make_whisper_client({
                    "host": self.whisper_host, "port": self.whisper_port,
                    "model": self.whisper_model,
                })
                await self.whisper.connect()

            server_info = await self.whisper.wait_until_ready(timeout=whisper_ready_timeout())
            backend = server_info.get("backend") or "unknown"
            device = server_info.get("device") or "unknown"
            compute_type = server_info.get("compute_type") or "unknown"
            model_name = server_info.get("model") or self.whisper_model
            if self._asr_model_id is not None and model_name != self._asr_model_id:
                raise ModelControlError("broadcast_model_changed",
                    "The ASR model changed outside this broadcast. Stop and restart the event to continue.", 409)
            self._asr_model_id = model_name
            self.logger.info(
                "✅ Whisper Live connected (language: %s, backend: %s, device: %s, precision: %s, model: %s)",
                forced_language,
                backend,
                device,
                compute_type,
                model_name,
            )

            # Broadcast to speaker terminal
            await broadcast_log(self.event_id, {
                "source": "whisper",
                "level": "success",
                "message": (
                    f"Whisper Live connected ({self.whisper_host}:{self.whisper_port}) → "
                    f"{self.target_language} | backend: {backend}, device: {device}, "
                    f"precision: {compute_type}, model: {model_name}, "
                    f"use_vad: {WHISPER_USE_VAD}, no_speech_thresh: {WHISPER_NO_SPEECH_THRESH}, "
                    f"active_timeout_s: {WHISPER_ACTIVE_TIMEOUT_S}, clip_audio: {WHISPER_CLIP_AUDIO}, "
                    f"same_output_threshold: {WHISPER_SAME_OUTPUT_THRESHOLD}, "
                    f"vad_parameters: {WHISPER_VAD_PARAMETERS}, "
                    f"max_uncommitted_audio_s: {WHISPER_MAX_UNCOMMITTED_AUDIO_S}, "
                    f"context_overlap_s: {WHISPER_CONTEXT_OVERLAP_S}, "
                    f"timeout_revision_buffer_ms: {WHISPER_TIMEOUT_REVISION_BUFFER_MS}"
                )
            })

            # Start transcription processor
            self._transcription_task = asyncio.create_task(self._process_transcriptions())

        except Exception as e:
            self.logger.error("Whisper Live connection failed (%s)", type(e).__name__)
            message = (str(e) if isinstance(e, ModelControlError) else
                       str(e.detail) if isinstance(e, HTTPException) and e.status_code == 409 else
                       "ASR could not start. Retry or check the selected model.")
            await broadcast_log(self.event_id, {
                "source": "whisper",
                "level": "error",
                "message": message
            })
            if self.whisper is not None:
                await self.whisper.close()
                self.whisper = None
            raise

    async def disconnect_whisper(self):
        """Disconnect from Whisper Live (for passthrough mode)."""
        if not self.owns_whisper:
            await self.finish_source_chunks()
            self._tts_text_buffer.clear()
            return

        self.logger.info(f"🔌 Disconnecting Whisper Live for {self.target_language}")

        if self._transcription_task and not self._transcription_task.done():
            self._transcription_task.cancel()
            try:
                await asyncio.wait_for(self._transcription_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._transcription_task = None

        if self.whisper:
            try:
                await self.whisper.close()
            except:
                pass
            self.whisper = None

        self._tts_text_buffer.clear()

    async def finish_whisper_audio(self):
        """End the current Whisper audio stream and let pending final text drain."""
        if not self.whisper:
            return

        self.logger.info(f"🏁 Finishing Whisper audio for {self.target_language}")

        try:
            await self.whisper.close()
        except Exception:
            pass
        finally:
            self.whisper = None

        if self._transcription_task and not self._transcription_task.done():
            try:
                await asyncio.wait_for(self._transcription_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                self._transcription_task.cancel()
                try:
                    await asyncio.wait_for(self._transcription_task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            finally:
                self._transcription_task = None

        self._tts_text_buffer.clear()

    async def ensure_whisper_connected(self):
        """Connect Whisper if this active agent owns ASR and has no live stream."""
        if not self.owns_whisper or not self.is_active:
            return
        if self.whisper and self.whisper.is_connected:
            return
        await self.disconnect_whisper()
        await self.connect_whisper()

    async def send_audio(self, audio_bytes: bytes):
        """Send audio to Whisper Live (called by session)."""
        if self.owns_whisper and self.whisper and self.is_active:
            try:
                await self.whisper.send_audio(audio_bytes)
            except:
                pass
    
    async def start_source_chunk_consumer(self):
        """Start consuming shared ASR chunks for translation/TTS."""
        if self.owns_whisper:
            return
        if self._source_chunk_queue is None:
            self._source_chunk_queue = asyncio.Queue()
        if self._source_chunk_task and not self._source_chunk_task.done():
            return
        self._source_chunk_task = asyncio.create_task(self._process_source_chunk_queue())

    async def enqueue_source_chunk(self, chunk):
        """Queue one shared source ASR chunk for this target agent."""
        if self.owns_whisper or not self.is_running or not self.is_active:
            return
        if self._source_chunk_queue is None:
            await self.start_source_chunk_consumer()
        await self._source_chunk_queue.put(chunk)

    async def finish_source_chunks(self):
        """Signal end-of-source chunks and wait for translation/TTS drain."""
        if self.owns_whisper:
            return
        if self._source_chunk_queue is not None:
            await self._source_chunk_queue.put(None)
        if self._source_chunk_task and not self._source_chunk_task.done():
            try:
                await asyncio.wait_for(self._source_chunk_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._source_chunk_task.cancel()
                try:
                    await asyncio.wait_for(self._source_chunk_task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
        self._source_chunk_task = None
        self._source_chunk_queue = None

    async def _source_chunk_stream(self):
        if self._source_chunk_queue is None:
            return
        while True:
            chunk = await self._source_chunk_queue.get()
            if chunk is None:
                break
            yield chunk

    async def _process_source_chunk_queue(self):
        await self._process_chunk_stream(self._source_chunk_stream())

    async def set_source_language(self, new_source: str):
        """
        Change source language.
        If source == target, enter passthrough mode. Shared-ASR target agents
        only toggle whether they consume source chunks; they never connect to
        WhisperLive directly.
        """
        async with self._lock:
            old_active = self.is_active
            self.source_language = new_source

            # Check if we should be active
            should_be_active = (new_source == "any" or new_source != self.target_language)

            if not self.owns_whisper:
                self.is_active = should_be_active
                if should_be_active and not old_active:
                    self.logger.info(f"▶️ Reactivating shared-ASR target (source: {new_source})")
                    await self.start_source_chunk_consumer()
                elif not should_be_active and old_active:
                    self.logger.info(f"⏸️ Entering passthrough mode (source == {self.target_language})")
                return

            if should_be_active and not old_active:
                # Reactivate: reconnect Whisper
                self.is_active = True
                self.logger.info(f"▶️ Reactivating agent (source: {new_source})")
                await self.connect_whisper()

            elif not should_be_active and old_active:
                # Deactivate: disconnect Whisper (passthrough mode)
                self.is_active = False
                self.logger.info(f"⏸️ Entering passthrough mode (source == {self.target_language})")
                await self.disconnect_whisper()

            elif should_be_active and old_active:
                # Still active but source changed: restart Whisper
                self.logger.info(f"🔄 Restarting Whisper (source: {new_source})")
                await self.disconnect_whisper()
                await asyncio.sleep(0.2)
                await self.connect_whisper()
    
    @staticmethod
    def _chunk_time_overlap_ratio(previous, current) -> float:
        previous_duration = max(0.0, previous.end_time - previous.start_time)
        current_duration = max(0.0, current.end_time - current.start_time)
        if previous_duration <= 0.0 or current_duration <= 0.0:
            return 0.0
        overlap = min(previous.end_time, current.end_time) - max(previous.start_time, current.start_time)
        if overlap <= 0.0:
            return 0.0
        return overlap / min(previous_duration, current_duration)

    @staticmethod
    def _chunk_text_key(chunk) -> str:
        normalized = re.sub(r"[^\w\s]", " ", (chunk.text or "").casefold())
        return " ".join(normalized.split())

    @classmethod
    def _chunk_text_similarity(cls, previous, current) -> float:
        previous_key = cls._chunk_text_key(previous)
        current_key = cls._chunk_text_key(current)
        if not previous_key or not current_key:
            return 0.0
        if previous_key == current_key:
            return 1.0
        return difflib.SequenceMatcher(None, previous_key, current_key).ratio()

    def _find_late_revision(self, released_timeouts, current, now: float):
        if current.is_partial:
            return None
        if current.finalization_reason not in {"stable", "bounded_window", "eos"}:
            return None

        best = None
        for released in released_timeouts:
            previous = released["chunk"]
            overlap = self._chunk_time_overlap_ratio(previous, current)
            if overlap < 0.65:
                continue
            similarity = self._chunk_text_similarity(previous, current)
            if similarity < 0.35:
                continue
            delta_ms = max(0.0, (now - released["released_at"]) * 1000.0)
            candidate = (previous, delta_ms, overlap, similarity)
            if best is None or overlap > best[2]:
                best = candidate
        return best

    def _log_late_revision(self, released_timeouts, current, now: float) -> None:
        if not LATENCY_LOGGING:
            return
        match = self._find_late_revision(released_timeouts, current, now)
        if match is None:
            return
        previous, delta_ms, overlap, similarity = match
        self.logger.info(
            "ASR_LATE_REVISION old_chunk=%s new_chunk=%s target=%s "
            "old_reason=%s new_reason=%s delta_ms=%.1f overlap=%.3f similarity=%.3f "
            "old_start_s=%.3f old_end_s=%.3f new_start_s=%.3f new_end_s=%.3f "
            "old_text=%r new_text=%r",
            previous.chunk_id,
            current.chunk_id,
            self.target_language,
            previous.finalization_reason or "unknown",
            current.finalization_reason or "unknown",
            delta_ms,
            overlap,
            similarity,
            previous.start_time,
            previous.end_time,
            current.start_time,
            current.end_time,
            previous.text,
            current.text,
        )

    def _remember_released_timeout(self, released_timeouts, chunk, released_at: float) -> None:
        released_timeouts.append({"chunk": chunk, "released_at": released_at})
        cutoff = released_at - 30.0
        del released_timeouts[:-20]
        while released_timeouts and released_timeouts[0]["released_at"] < cutoff:
            released_timeouts.pop(0)

    def _should_buffer_final_chunk(self, chunk) -> bool:
        return (
            self._timeout_revision_buffer_s > 0.0
            and not chunk.is_partial
            and chunk.finalization_reason == "timeout"
        )

    def _supersedes_buffered_timeout(self, pending, current) -> bool:
        if current.is_partial:
            return False
        if current.finalization_reason not in {"stable", "bounded_window", "eos"}:
            return False
        return self._chunk_time_overlap_ratio(pending, current) >= 0.65

    async def _revision_buffered_transcriptions(self) -> AsyncIterator[Any]:
        """Hold timeout finals briefly so immediate stable confirmations can replace them."""
        stream = self.whisper.receive_transcriptions().__aiter__()
        loop = asyncio.get_running_loop()
        pending_timeout = None
        pending_deadline = 0.0
        next_chunk_task = None
        released_timeouts = []

        try:
            while True:
                if next_chunk_task is None:
                    next_chunk_task = asyncio.create_task(stream.__anext__())

                if pending_timeout is not None:
                    receive_timeout = max(0.0, pending_deadline - loop.time())
                    done, _ = await asyncio.wait({next_chunk_task}, timeout=receive_timeout)
                    if not done:
                        if LATENCY_LOGGING:
                            self.logger.info(
                                "ASR_REVISION_RELEASED chunk=%s target=%s delay_ms=%.1f",
                                pending_timeout.chunk_id,
                                self.target_language,
                                self._timeout_revision_buffer_s * 1000.0,
                            )
                        self._remember_released_timeout(released_timeouts, pending_timeout, loop.time())
                        yield pending_timeout
                        pending_timeout = None
                        continue
                else:
                    done, _ = await asyncio.wait({next_chunk_task})

                try:
                    chunk = next_chunk_task.result()
                except StopAsyncIteration:
                    if pending_timeout is not None:
                        if LATENCY_LOGGING:
                            self.logger.info(
                                "ASR_REVISION_RELEASED chunk=%s target=%s delay_ms=stream_end",
                                pending_timeout.chunk_id,
                                self.target_language,
                            )
                        self._remember_released_timeout(released_timeouts, pending_timeout, loop.time())
                        yield pending_timeout
                    break
                finally:
                    next_chunk_task = None

                if pending_timeout is not None:
                    if self._supersedes_buffered_timeout(pending_timeout, chunk):
                        if LATENCY_LOGGING:
                            self.logger.info(
                                "ASR_REVISION_SUPERSEDED old_chunk=%s new_chunk=%s target=%s "
                                "old_reason=%s new_reason=%s overlap=%.3f",
                                pending_timeout.chunk_id,
                                chunk.chunk_id,
                                self.target_language,
                                pending_timeout.finalization_reason or "unknown",
                                chunk.finalization_reason or "unknown",
                                self._chunk_time_overlap_ratio(pending_timeout, chunk),
                            )
                        pending_timeout = None
                    elif chunk.is_partial:
                        yield chunk
                        continue
                    else:
                        if LATENCY_LOGGING:
                            self.logger.info(
                                "ASR_REVISION_RELEASED chunk=%s target=%s delay_ms=%.1f reason=next_final",
                                pending_timeout.chunk_id,
                                self.target_language,
                                max(0.0, (loop.time() - (pending_deadline - self._timeout_revision_buffer_s)) * 1000.0),
                            )
                        self._remember_released_timeout(released_timeouts, pending_timeout, loop.time())
                        yield pending_timeout
                        pending_timeout = None

                self._log_late_revision(released_timeouts, chunk, loop.time())

                if self._should_buffer_final_chunk(chunk):
                    pending_timeout = chunk
                    pending_deadline = loop.time() + self._timeout_revision_buffer_s
                    if LATENCY_LOGGING:
                        self.logger.info(
                            "ASR_REVISION_BUFFERED chunk=%s target=%s reason=timeout delay_ms=%.1f "
                            "start_s=%.3f end_s=%.3f",
                            chunk.chunk_id,
                            self.target_language,
                            self._timeout_revision_buffer_s * 1000.0,
                            chunk.start_time,
                            chunk.end_time,
                        )
                    continue

                yield chunk
        finally:
            if next_chunk_task is not None and not next_chunk_task.done():
                next_chunk_task.cancel()

    async def _fanout_source_chunk(self, chunk):
        """Send one finalized shared ASR chunk to all active target agents."""
        if chunk.is_partial:
            return
        for agent in self.fanout_agents:
            if agent.is_active:
                await agent.enqueue_source_chunk(chunk)

    async def _finish_fanout_agents(self):
        """Signal all target agents that the shared ASR stream has ended."""
        for agent in self.fanout_agents:
            await agent.finish_source_chunks()

    async def _process_transcriptions(self):
        """Process the owned WhisperLive stream and fan out finalized chunks."""
        try:
            await self._process_chunk_stream(self._revision_buffered_transcriptions())
        finally:
            if self.owns_whisper and self.fanout_agents:
                await self._finish_fanout_agents()

    async def _process_chunk_stream(self, chunks: AsyncIterator[Any]):
        """Process source chunks from either owned ASR or the shared-ASR queue."""
        async def reconciled_chunks():
            async for chunk in chunks:
                if not self.is_running or not self.is_active:
                    break
                if self.owns_whisper:
                    chunk = self._caption_revisions.receive(chunk)
                    if chunk is None:
                        continue
                yield chunk

        async def publish_prefix(chunk):
            # Also preserves immediate CA captions in legacy duplicated-ASR mode.
            await self._handle_transcription_chunk(chunk, source_only=True)
            if self.owns_whisper:
                await self._fanout_source_chunk(chunk)

        async def inputs():
            source = reconciled_chunks()
            if self.target_language in {"es", "en"} and self._translation_max_wait_s > 0:
                grouped = group_prefix_translations(
                    source, max_wait_s=self._translation_max_wait_s, on_prefix=publish_prefix,
                )
                translated = sentence_translation_inputs(grouped) if self._translation_sentence_context else grouped
                try:
                    async for item in translated:
                        yield item
                finally:
                    await translated.aclose()
                    await grouped.aclose()
            else:
                async for chunk in source:
                    yield TranslationInput(chunk)

        stream = inputs()
        try:
            async for item in stream:
                await self._handle_transcription_chunk(
                    item.chunk, translation_only=item.grouping is not None,
                    translation_group=item.grouping,
                )
                if self.owns_whisper and item.grouping is None:
                    await self._fanout_source_chunk(item.chunk)

            await self._flush_tts_and_log_end()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self.owns_whisper:
                await self._report_asr_failure(e)
            else:
                self.logger.error("Translation stream failed: %s", type(e).__name__)
        finally:
            await stream.aclose()
            await self._close_caption_revision("stream_end")

    async def _report_asr_failure(self, error):
        failure = error if isinstance(error, ASRStreamError) else ASRStreamError("asr_stream_failed")
        self.asr_error = {"code": failure.code, "message": str(failure), "stream_id": self._caption_stream_id}
        self.is_active = False
        self.logger.error("ASR_STREAM_FAILED code=%s error_type=%s", failure.code, type(error).__name__)
        await broadcast_log(self.event_id, {"source": "whisper", "level": "error",
            "message": str(failure), "asr_failed": True, "asr_error_code": failure.code})
        payload = json.dumps({"type": "asr_status", "status": "failed", **self.asr_error})
        if self.room:
            try:
                await self.room.local_participant.publish_data(payload.encode(), reliable=True, topic="asr.status")
            except Exception as exc:
                self.logger.warning("ASR status delivery failed: %s", type(exc).__name__)
        code = event_id_to_code.get(self.event_id)
        if code:
            languages = {"original", self.target_language}
            languages.update(agent.target_language for agent in self.fanout_agents)
            for language in languages:
                await captions_manager.broadcast(code, language, payload)

    async def _handle_transcription_chunk(self, chunk, *, source_only=False,
                                          translation_only=False, translation_group=None):
        """
        Process one source transcription chunk.

        Source-ASR owner agents publish the original/source subtitle. Target
        agents translate the same final source chunk and publish/TTS the target
        language output.
        """
        if not translation_only:
            self._processed_chunk_count += 1
        translated_text = None
        audio_start_s = None if chunk.prefix_protocol else chunk.start_time
        audio_end_s = None if chunk.prefix_protocol else chunk.end_time

        text = chunk.text.strip() if chunk.text else ""
        if not text:
            return

        # Get punctuation
        punct = chunk.punctuation if chunk.punctuation else ""
        if not punct and text[-1:] in '.?!,;:':
            punct = text[-1]

        # ============================================================
        # 1. SUBTITLE PROCESSING
        # ============================================================
        if self.room:
            try:
                if chunk.is_partial:
                    # PARTIAL: Show growing original text in real-time
                    if self.is_primary_agent and not translation_only:
                        self._original_partial_buffer = text
                        await self._send_subtitle(
                            channel="original",
                            text=text,
                            is_partial=True,
                            is_translation=False,
                            revision_key=(
                                f"asr-timeout:{self._timeout_revision_generation}"
                                if self._timeout_revision_active and self._timeout_revision_chunk
                                and self._chunk_time_overlap_ratio(self._timeout_revision_chunk, chunk) >= 0.65
                                else ""
                            ),
                            chunk_id=chunk.chunk_id,
                            audio_start_s=audio_start_s,
                            audio_end_s=audio_end_s,
                        )

                else:
                    # FINAL: Confirm original and translate
                    final_text = text
                    close_timeout_revision = False
                    revision_closed = chunk.finalization_reason not in {"timeout", "bounded_window"}
                    if (revision_closed and self._timeout_revision_active
                            and self._timeout_revision_chunk
                            and self._chunk_time_overlap_ratio(self._timeout_revision_chunk, chunk) >= 0.65
                            and self._chunk_text_key(self._timeout_revision_chunk) == self._chunk_text_key(chunk)):
                        # The reconciled source did not change. Close the
                        # existing caption without translating/speaking it again.
                        await self._close_caption_revision("confirmed", chunk.finalization_reason)
                        return
                    if (self._timeout_revision_active and self._timeout_revision_chunk
                            and self._chunk_time_overlap_ratio(self._timeout_revision_chunk, chunk) < 0.65):
                        # A different audio span must not replace the preceding draft.
                        await self._close_caption_revision("next_chunk")
                    if not revision_closed:
                        if not self._timeout_revision_active:
                            self._timeout_revision_generation += 1
                            self._timeout_revision_active = True
                        revision_key = f"asr-timeout:{self._timeout_revision_generation}"
                        self._timeout_revision_chunk = chunk
                    elif self._timeout_revision_active:
                        # A stable/eos result confirms the current timeout hypothesis.
                        revision_key = f"asr-timeout:{self._timeout_revision_generation}"
                        close_timeout_revision = True
                    else:
                        revision_key = ""
                    self.logger.debug(f"📤 FINAL: '{final_text[:60]}...'")
                    if LATENCY_LOGGING and self.owns_whisper and not translation_only:
                        latency_label = (
                            f"{chunk.latency_ms:.1f}"
                            if chunk.latency_ms is not None else "unavailable"
                        )
                        self.logger.info(
                            "ASR_FINAL chunk=%s target=%s finalization=%s "
                            "start_s=%.3f end_s=%.3f latency_ms=%s chars=%d text=%r",
                            chunk.chunk_id,
                            self.target_language,
                            chunk.finalization_reason or "unknown",
                            chunk.start_time,
                            chunk.end_time,
                            latency_label,
                            len(final_text),
                            final_text,
                        )

                    # 1a. Send confirmed ORIGINAL subtitle once, from source ASR owner.
                    if self.is_primary_agent and not translation_only:
                        self._original_confirmed.append(final_text)
                        if len(self._original_confirmed) > self._max_confirmed_sentences:
                            self._original_confirmed = self._original_confirmed[-self._max_confirmed_sentences:]

                        await self._send_subtitle(
                            channel="original",
                            text=final_text,
                            is_partial=False,
                            is_translation=False,
                            revision_key=revision_key,
                            finalization_reason=chunk.finalization_reason,
                            revision_closed=revision_closed,
                            chunk_id=chunk.chunk_id,
                            audio_start_s=audio_start_s,
                            audio_end_s=audio_end_s,
                        )
                        self._original_partial_buffer = ""

                        # Broadcast to speaker terminal
                        await broadcast_log(self.event_id, {
                            "source": "whisper",
                            "level": "info",
                            "message": f"[Transcription] {final_text}",
                            "replace_key": revision_key,
                        })

                        if LATENCY_LOGGING and chunk.latency_ms is not None:
                            await broadcast_log(self.event_id, {
                                "source": "whisper",
                                "level": "info",
                                "message": (
                                    f"[ASR] chunk={chunk.chunk_id} "
                                    f"latency={chunk.latency_ms / 1000.0:.2f}s "
                                    f"finalization={chunk.finalization_reason} "
                                    f"revision={revision_key or 'none'} "
                                    f"text=\"{final_text}\""
                                )
                            })
                        elif LATENCY_LOGGING:
                            await broadcast_log(self.event_id, {
                                "source": "whisper",
                                "level": "warning",
                                "message": (
                                    f"[ASR] chunk={chunk.chunk_id} latency=unavailable "
                                    f"finalization={chunk.finalization_reason} "
                                    f"revision={revision_key or 'none'} "
                                    f"segment_end={chunk.end_time:.3f}s "
                                    f"text=\"{final_text}\""
                                )
                            })

                    # 1b. TRANSLATE and send translated subtitle.
                    if self.target_language != "original" and not source_only:
                        sentence_key = (translation_group or {}).get("sentence_key", "")
                        if sentence_key:
                            revision_key = sentence_key
                            revision_closed = translation_group["sentence_closed"]
                        if translation_group is not None and not translation_group.get("close_only"):
                            self.logger.info(
                                "TRANSLATION_GROUP chunk=%s target=%s reason=%s chunks=%d "
                                "words=%d buffer_wait_ms=%.1f max_wait_ms=%.1f",
                                chunk.chunk_id, self.target_language, translation_group["reason"],
                                len(translation_group["source_chunk_ids"]), len(final_text.split()),
                                translation_group["buffer_wait_ms"], translation_group["max_wait_ms"],
                            )
                        translated_text = final_text
                        translation_status = "fallback"
                        translation_error = "translator_unavailable"
                        original_language = chunk.language or self.source_language
                        if original_language in {"any", "auto"}:
                            original_language = ""
                        original_label = f" ({original_language.upper()})" if original_language else ""
                        translation_started_at = time.monotonic()
                        cached = self._caption_drafts.get(self.target_language)
                        reuse_translation = bool(sentence_key and translation_group.get("close_only")
                            and cached and cached["revision_key"] == sentence_key)
                        if reuse_translation:
                            translated_text = cached["text"]
                            translation_status = cached["translation_status"]
                            translation_error = cached["translation_error"]
                            original_language = cached["text_language"] if translation_status == "fallback" else original_language
                            self.logger.info("TRANSLATION_CONTEXT_CLOSE key=%s target=%s reason=%s",
                                sentence_key, self.target_language, translation_group["sentence_close_reason"])
                        if self.translator and not reuse_translation:
                            try:
                                translate_with_status = getattr(self.translator, "translate_with_status", None)
                                if callable(translate_with_status):
                                    result = await translate_with_status(final_text, chunk_id=chunk.chunk_id)
                                    translated_text = result.text
                                    translation_status = result.status
                                    translation_error = result.error
                                else:
                                    # Compatibility for existing third-party translators.
                                    translated_text = await self.translator.translate(final_text)
                                    translation_status = "translated"
                                    translation_error = ""
                                if LATENCY_LOGGING:
                                    self.logger.info(
                                        "TRANSLATION_DONE chunk=%s target=%s duration_ms=%.1f chars_in=%d chars_out=%d status=%s error=%s",
                                        chunk.chunk_id,
                                        self.target_language,
                                        (time.monotonic() - translation_started_at) * 1000.0,
                                        len(final_text),
                                        len(translated_text),
                                        translation_status,
                                        translation_error or "none",
                                    )
                                # Broadcast translation to speaker terminal
                                if translation_status == "translated" and translated_text != final_text:
                                    await broadcast_log(self.event_id, {
                                        "source": "translator",
                                        "level": "info",
                                        "message": f"[{self.target_language.upper()}] {translated_text}",
                                        "replace_key": (
                                            f"translation:{self.target_language}:{revision_key}"
                                            if revision_key else ""
                                        ),
                                    })
                            except Exception as e:
                                import traceback
                                self.logger.error(
                                    "TRANSLATION_FALLBACK chunk=%s target=%s error_type=%s\n%s",
                                    chunk.chunk_id, self.target_language, type(e).__name__,
                                    "".join(traceback.format_tb(e.__traceback__)),
                                )
                                translated_text = final_text  # Fallback: use original
                                translation_status = "fallback"
                                translation_error = "translator_error"

                        if translation_status == "fallback":
                            await broadcast_log(self.event_id, {
                                "source": "translator",
                                "level": "warning",
                                "message": (
                                    f"[{self.target_language.upper()}] Translation unavailable; "
                                    f"showing original{original_label}. "
                                    f"Reason: {translation_error}; chunk: {chunk.chunk_id}"
                                ),
                            })

                        if not sentence_key or revision_closed:
                            self._translation_confirmed.append(translated_text)
                        if len(self._translation_confirmed) > self._max_confirmed_sentences:
                            self._translation_confirmed = self._translation_confirmed[-self._max_confirmed_sentences:]

                        await self._send_subtitle(
                            channel=self.target_language,
                            text=translated_text,
                            is_partial=bool(sentence_key and not revision_closed),
                            is_translation=True,
                            revision_key=revision_key,
                            finalization_reason=chunk.finalization_reason,
                            revision_closed=revision_closed,
                            chunk_id=chunk.chunk_id,
                            audio_start_s=audio_start_s,
                            audio_end_s=audio_end_s,
                            translation_status=translation_status,
                            translation_error=translation_error,
                            text_language=original_language if translation_status == "fallback" else self.target_language,
                            translation_group=translation_group,
                            closure_reason=translation_group.get("sentence_close_reason", "") if sentence_key else "",
                        )
                        self._translation_partial_buffer = ""

                    if close_timeout_revision:
                        self._timeout_revision_active = False
                        self._timeout_revision_chunk = None

            except Exception as e:
                self.logger.debug(f"Subtitle error: {e}")

        # ============================================================
        # 2. PACE METER
        # ============================================================
        if not chunk.is_partial and not translation_only:
            self._pace_meter.add_chunk(text)

        # ============================================================
        # 3. TTS GENERATION - parallel with ordering (use translated text)
        # ============================================================
        sentence_draft = bool(translation_group and translation_group.get("sentence_key")
                              and not translation_group["sentence_closed"])
        if not chunk.is_partial and text and self.audio_publisher and not source_only and not sentence_draft:
            tts_text = translated_text if translated_text else text
            text_to_synth = self._tts_text_buffer.add_chunk(tts_text, punct)

            if text_to_synth:
                self._tts_count += 1
                self._pace_meter.set_backlog_boost(self._tts_pending)
                rate = self._pace_meter.get_speaking_rate()

                seq_num = self._tts_sequence
                self._tts_sequence += 1

                task = asyncio.create_task(
                    self._generate_tts(text_to_synth, rate, seq_num)
                )
                self._tts_tasks.append(task)
                self._tts_tasks = [t for t in self._tts_tasks if not t.done()]

    async def _flush_tts_and_log_end(self):
        await self._close_caption_revision("stream_end")
        # Flush TTS buffer at end (only if TTS is active)
        remaining = self._tts_text_buffer.flush()
        if remaining and self.audio_publisher:
            seq_num = self._tts_sequence
            self._tts_sequence += 1
            task = asyncio.create_task(
                self._generate_tts(remaining, self._pace_meter.get_speaking_rate(), seq_num)
            )
            self._tts_tasks.append(task)

        if self._tts_tasks:
            self.logger.info(f"⏳ Waiting for {len(self._tts_tasks)} TTS tasks to complete...")
            await asyncio.gather(*self._tts_tasks, return_exceptions=True)
            self._tts_tasks = []

        self.logger.info(f"Transcription ended: {self._processed_chunk_count} chunks, {self._tts_count} TTS")

    async def _close_caption_revision(self, reason: str, finalization_reason: str = ""):
        """Commit the latest caption draft without retranslating or repeating TTS."""
        for channel, draft in list(self._caption_drafts.items()):
            if finalization_reason:
                draft = dict(draft, finalization_reason=finalization_reason)
            await self._send_subtitle(
                channel=channel, is_partial=False, revision_closed=True,
                closure_reason=reason, **draft,
            )
        self._timeout_revision_active = False
        self._timeout_revision_chunk = None

    async def _send_subtitle(
        self,
        channel: str,
        text: str,
        is_partial: bool,
        is_translation: bool,
        revision_key: str = "",
        finalization_reason: str = "",
        revision_closed: bool = False,
        chunk_id: str = "",
        closure_reason: str = "",
        audio_start_s: Optional[float] = None,
        audio_end_s: Optional[float] = None,
        translation_status: str = "",
        translation_error: str = "",
        text_language: str = "",
        translation_group: Optional[dict] = None,
    ):
        """
        Send subtitle text via data channel and WebSocket.

        Uses LiveKit topics for scalable filtering:
        - Topic: "subtitle.{channel}" (e.g., "subtitle.es", "subtitle.original")
        - Clients can subscribe to specific topics to receive only relevant data

        Args:
            channel: "original" for transcription, or language code for translation (e.g. "es", "en")
            text: The subtitle text (accumulated for partials, final sentence for finals)
            is_partial: True if this is a partial (growing text), False if final (confirmed)
            is_translation: True if translation, False if original transcription
        """
        if not text:
            return

        sentence_translation = bool(translation_group and translation_group.get("sentence_key"))
        if not is_partial or sentence_translation:
            if revision_closed:
                self._caption_drafts.pop(channel, None)
            else:
                self._caption_drafts[channel] = {
                    "text": text, "is_translation": is_translation,
                    "revision_key": revision_key, "finalization_reason": finalization_reason,
                    "chunk_id": chunk_id,
                    "audio_start_s": audio_start_s, "audio_end_s": audio_end_s,
                    "translation_status": translation_status,
                    "translation_error": translation_error,
                    "text_language": text_language,
                    "translation_group": translation_group,
                }
        self._caption_sequence += 1

        # Build the subtitle data
        subtitle_data = {
            "type": "subtitle",
            "channel": channel,
            "language": channel if channel != "original" else self.source_language,
            "text": text,
            "is_partial": is_partial,
            "is_final": not is_partial,
            "is_translation": is_translation,
            "is_original": not is_translation,
            "revision_key": revision_key,
            # is_final remains backwards compatible; timeout finals are revisable.
            "finalization_reason": finalization_reason or ("partial" if is_partial else "stable"),
            "revision_closed": revision_closed and not is_partial,
            "closure_reason": closure_reason,
            "chunk_id": chunk_id,
            "audio_start_s": audio_start_s,
            "audio_end_s": audio_end_s,
            "caption_sequence": self._caption_sequence,
            "stream_id": self._caption_stream_id,
            "timestamp": time.time(),
            "agent_id": f"agent-{self.target_language}",
        }
        if translation_status:
            subtitle_data.update({
                "translation_status": translation_status,
                "translation_fallback": translation_status == "fallback",
                "translation_error": translation_error,
                "text_language": text_language,
            })
        if translation_group is not None:
            subtitle_data["translation_group"] = translation_group

        # Build topic for scalable filtering: "subtitle.es", "subtitle.original", etc.
        topic = f"{TOPIC_SUBTITLE_PREFIX}.{channel}"

        # Send via LiveKit data channel with topic
        if self.room:
            await self.room.local_participant.publish_data(
                json.dumps(subtitle_data).encode(),
                reliable=not is_partial or sentence_translation,  # MT revisions must arrive in order.
                topic=topic,  # Topic for client-side filtering
            )

        # Also broadcast to WebSocket captions clients
        event_code = event_id_to_code.get(self.event_id)
        if event_code:
            await captions_manager.broadcast(event_code, channel, json.dumps(subtitle_data))
    
    async def _generate_tts(self, text: str, speaking_rate: float = 1.2, sequence_num: int = 0):
        """
        Generate TTS and send to AudioPublisher amb ORDRE.
        
        Args:
            text: Text a sintetitzar
            speaking_rate: Velocitat
            sequence_num: Número de seqüència per mantenir ordre
        """
        import time
        from utils.text_normalizer import normalize_text_for_tts
        
        if not self.audio_publisher or not self.is_active:
            return
        
        self._tts_pending += 1
        start_time = time.time()
        
        try:
            # Accelerar si hi ha cola
            self._pace_meter.set_backlog_boost(self._tts_pending)
            adjusted_rate = self._pace_meter.get_speaking_rate()
            
            # Normalitzar text
            normalized = normalize_text_for_tts(text, self.target_language)
            if not normalized.strip():
                return
            
            # Sintetitzar
            audio_data, duration = await self.tts.synthesize(normalized, speaking_rate=adjusted_rate)
            
            synth_time = time.time() - start_time
            
            if audio_data:
                # Enviar a LiveKit amb ORDRE (seqüència)
                # Fade-in només per Chirp3
                await self.audio_publisher.queue_audio_ordered(
                    audio_data,
                    sequence_num,
                    apply_fade=True
                )
                
                # Log amb mètriques
                self.logger.info(
                    f"🔊 TTS[{sequence_num}]: {len(normalized)} chars → {duration:.1f}s "
                    f"(synth: {synth_time:.2f}s, rate: {adjusted_rate:.2f}x, pending: {self._tts_pending})"
                )

                # Broadcast to speaker terminal
                await broadcast_log(self.event_id, {
                    "source": "google",
                    "level": "info",
                    "message": f"[TTS {self.target_language.upper()}] {len(normalized)} chars → {duration:.1f}s (rate: {adjusted_rate:.2f}x)"
                })
                    
        except Exception as e:
            self.logger.error(f"TTS error: {e}")
        finally:
            self._tts_pending -= 1
    
    async def stop(self):
        """Stop the agent."""
        # Drain confirmed translation inputs while the worker is still active.
        # Marking it inactive first would skip the tail at the stream-end flush.
        if self.owns_whisper:
            await self.finish_whisper_audio()
        await self.finish_source_chunks()
        self.is_running = False
        self.is_active = False
        
        # Cancel·lar tasques TTS pendents
        for task in self._tts_tasks:
            if not task.done():
                task.cancel()
        self._tts_tasks = []
        
        await self.disconnect_whisper()
        self.logger.info(f"🛑 Agent stopped")


# ============================================================================
# Session Manager Functions
# ============================================================================

async def setup_session_livekit(session: TranslationSession):
    """
    Set up LiveKit connection for session.
    Creates ALL tracks upfront (never deleted during session).
    
    FIX: Added lock to prevent race condition when broadcaster track arrives
    both from existing participants and track_subscribed event.
    """
    from livekit import rtc
    
    logger.info(f"📡 Setting up LiveKit for session {session.event_id}")
    
    # Initialize audio stream guard and lock
    session._audio_stream_active = False
    session._audio_stream_lock = asyncio.Lock()
    session._starting_audio_processor = False  # Flag to prevent duplicate starts
    
    async def start_audio_processor_safe():
        """Safely start audio processor, preventing duplicates."""
        async with session._audio_stream_lock:
            # Double-check: if already active or starting, skip
            if session._audio_stream_active or session._starting_audio_processor:
                logger.warning("⚠️ Audio processor already active/starting, skipping")
                return
            
            # Cancel existing task if any
            if session.audio_processor_task and not session.audio_processor_task.done():
                session.audio_processor_task.cancel()
                try:
                    await asyncio.wait_for(session.audio_processor_task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                logger.info("⚠️ Cancelled previous audio processor task")
            
            # Mark as starting
            session._starting_audio_processor = True
        
        # Start new processing task (outside lock to avoid deadlock)
        session.audio_processor_task = asyncio.create_task(
            process_broadcaster_audio(session)
        )
    
    # Connect to room as session server
    session.room = rtc.Room()
    
    @session.room.on("track_subscribed")
    def on_track_subscribed(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO and "broadcaster" in participant.identity.lower():
            session.broadcaster_track = track
            logger.info(f"📡 Got broadcaster track!")
            
            # Start audio processor safely
            asyncio.create_task(start_audio_processor_safe())
    
    @session.room.on("track_unsubscribed")
    def on_track_unsubscribed(track, publication, participant):
        if "broadcaster" in participant.identity.lower():
            session.broadcaster_track = None
            logger.info(f"📡 Broadcaster track unsubscribed")
            # Cancel audio processing
            if session.audio_processor_task and not session.audio_processor_task.done():
                session.audio_processor_task.cancel()
    
    # Connect
    identity = f"session-server-{session.event_id[:8]}"
    token = create_token(session.room_name, identity, can_publish=True, can_subscribe=True)
    await session.room.connect(LIVEKIT_INTERNAL_URL, token)
    logger.info(f"✅ Session server connected to room: {session.room_name}")
    
    # Create AudioSource and AudioPublisher for EACH target language
    for lang in session.target_languages:
        # Create AudioSource at 48kHz (LiveKit standard)
        audio_source = rtc.AudioSource(48000, 1)
        session.audio_sources[lang] = audio_source
        
        # Create and publish track
        track_name = f"tts-{lang}"
        track = rtc.LocalAudioTrack.create_audio_track(track_name, audio_source)
        
        # Configurar opciones de publicación para mejor calidad
        # NOTA: El nombre del track se establece en create_audio_track() (línea anterior)
        # El track se publica con nombre "tts-{lang}" (ej: tts-es, tts-en, tts-ca)
        publish_options = rtc.TrackPublishOptions()
        publish_options.source = rtc.TrackSource.SOURCE_MICROPHONE  # Identificar como fuente de audio
        publish_options.dtx = True   # Discontinuous Transmission (ahorra ancho de banda en silencio)
        publish_options.red = True   # Redundant Encoding (mejora calidad en redes con pérdida)
        
        publication = await session.room.local_participant.publish_track(track, publish_options)

        # Debug: Log track name from publication to verify it's set correctly
        pub_name = getattr(publication, 'name', 'NO_NAME_ATTR')
        track_actual_name = getattr(track, 'name', 'NO_NAME_ATTR')
        pub_sid = getattr(publication, 'sid', 'NO_SID')
        logger.info(f"📤 Published track: {track_name} @ 48kHz (DTX=on, RED=on)")
        logger.info(f"   └─ Track.name={track_actual_name}, Publication.name={pub_name}, SID={pub_sid}")
        
        # Save track info to database for external services
        try:
            track_data = {
                "event_id": session.event_id,
                "room_name": session.room_name,
                "livekit_url": LIVEKIT_URL,
                "track_sid": publication.sid if hasattr(publication, 'sid') else None,
                "track_name": track_name,
                "track_type": "audio",
                "track_source": "agent",
                "participant_identity": identity,
                "language": lang,
                "is_agent_track": True,
                "is_active": True,
            }
            
            # Find channel_id for this language
            if supabase:
                channel_result = supabase.table("channels").select("id").eq("event_id", session.event_id).eq("language", lang).execute()
                if channel_result.data and len(channel_result.data) > 0:
                    track_data["channel_id"] = channel_result.data[0]["id"]
                
                # Upsert track (insert or update if exists)
                supabase.table("livekit_tracks").upsert(track_data, on_conflict="event_id,track_name").execute()
                logger.info(f"💾 Saved track info: {track_name}")
        except Exception as e:
            logger.warning(f"⚠️ Could not save track to DB: {e}")
        
        # Create AudioPublisher (handles TTS 24kHz → 48kHz resampling with native LiveKit resampler)
        publisher = AudioPublisher(audio_source, tts_sample_rate=24000)
        await publisher.start()
        session.audio_publishers[lang] = publisher
        logger.info(f"🔊 AudioPublisher started for {lang} (native resampler)")
    
    # Check for existing broadcaster (only if not already started by track_subscribed)
    if not session._starting_audio_processor:
        for participant in session.room.remote_participants.values():
            if "broadcaster" in participant.identity.lower():
                for pub in participant.track_publications.values():
                    if pub.track and pub.kind == rtc.TrackKind.KIND_AUDIO:
                        session.broadcaster_track = pub.track
                        logger.info(f"📡 Found existing broadcaster")
                        await start_audio_processor_safe()
                        break


async def process_broadcaster_audio(session: TranslationSession):
    """
    Process audio from broadcaster and distribute to agents/channels.
    
    NATIVE LIVEKIT RESAMPLER VERSION:
    - Uses rtc.AudioResampler for 48kHz → 16kHz (Whisper Live)
    - Maintains state between frames (no discontinuities)
    - For passthrough: send original frame directly (NO processing)
    - Guard against multiple AudioStream instances using lock
    """
    from livekit import rtc
    
    if not session.broadcaster_track:
        return
    
    # Guard: Check if already processing (thread-safe with lock)
    if hasattr(session, '_audio_stream_lock'):
        async with session._audio_stream_lock:
            if session._audio_stream_active:
                logger.warning("⚠️ AudioStream already active, skipping duplicate")
                return
            session._audio_stream_active = True
            session._starting_audio_processor = False  # Reset flag
    else:
        # Fallback for sessions without lock
        if hasattr(session, '_audio_stream_active') and session._audio_stream_active:
            logger.warning("⚠️ AudioStream already active, skipping duplicate")
            return
        session._audio_stream_active = True
    
    logger.info(f"🎧 Starting broadcaster audio processing for {session.event_id}")

    if session.source_asr_agent:
        await session.source_asr_agent.ensure_whisper_connected()
    else:
        for agent in session.whisper_agents.values():
            await agent.ensure_whisper_connected()
    
    # Create SINGLE audio stream at 48kHz
    audio_stream = rtc.AudioStream(
        session.broadcaster_track,
        sample_rate=48000,
        num_channels=1,
    )
    
    # Create native resampler for Whisper Live (48kHz → 16kHz)
    # VERY_HIGH quality, maintains state between frames
    whisper_resampler = rtc.AudioResampler(
        input_rate=48000,
        output_rate=16000,
        num_channels=1,
        quality=rtc.AudioResamplerQuality.VERY_HIGH,
    )
    
    frame_count = 0
    recording_file = None
    recording_path = None
    
    try:
        async for event in audio_stream:
            if not session.is_active:
                break
            
            frame_count += 1
            frame = event.frame
            
            if frame_count == 1:
                logger.info(f"🎵 First frame: {frame.sample_rate}Hz, {frame.num_channels}ch, {frame.samples_per_channel} samples")
                try:
                    event_dir = BACKSTAGE_RECORDINGS_DIR / session.event_id
                    event_dir.mkdir(parents=True, exist_ok=True)
                    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                    recording_path = event_dir / f"source_{timestamp}_{uuid.uuid4().hex[:8]}.wav"
                    recording_file = wave.open(str(recording_path), "wb")
                    recording_file.setnchannels(frame.num_channels)
                    recording_file.setsampwidth(2)
                    recording_file.setframerate(frame.sample_rate)
                    logger.info(f"💾 Recording source audio to {recording_path}")
                except Exception as e:
                    recording_file = None
                    recording_path = None
                    logger.warning(f"⚠️ Could not start source audio recording: {e}")

            if recording_file:
                try:
                    recording_file.writeframes(bytes(frame.data))
                except Exception as e:
                    logger.warning(f"⚠️ Could not write source audio frame: {e}")
                    try:
                        recording_file.close()
                    except Exception:
                        pass
                    recording_file = None
            
            # Resample to 16kHz for Whisper Live using NATIVE resampler
            # This maintains state between frames, preventing discontinuities
            resampled_frames = whisper_resampler.push(frame)
            
            # Combine all resampled frames into one audio chunk for Whisper Live
            audio_16k_chunks = []
            for rf in resampled_frames:
                audio_16k_chunks.append(bytes(rf.data))
            
            audio_16k = b''.join(audio_16k_chunks) if audio_16k_chunks else None
            
            # Shared mode sends once; duplicated mode sends to every active target.
            if audio_16k:
                if session.source_asr_agent and session.source_asr_agent.is_active:
                    await session.source_asr_agent.send_audio(audio_16k)
                elif not session.source_asr_agent:
                    for agent in session.whisper_agents.values():
                        if agent.is_active:
                            await agent.send_audio(audio_16k)

            # Passthrough channels still receive the original 48kHz frame directly.
            for lang, agent in session.whisper_agents.items():
                if not agent.is_active and lang in session.audio_publishers:
                    await session.audio_publishers[lang].queue_passthrough_audio(frame)
        
        logger.info(f"Audio stream ended ({frame_count} frames)")
        
    except asyncio.CancelledError:
        logger.info("Audio processing cancelled")
    except Exception as e:
        logger.error(f"Audio processing error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if recording_file:
            try:
                recording_file.close()
                logger.info(f"💾 Source audio recording saved: {recording_path}")
            except Exception as e:
                logger.warning(f"⚠️ Could not finalize source audio recording: {e}")
        if session.source_asr_agent and session.source_asr_agent.is_active:
            await session.source_asr_agent.finish_whisper_audio()
        for agent in session.whisper_agents.values():
            if agent.is_active and agent.owns_whisper:
                await agent.finish_whisper_audio()
            elif agent.is_active:
                await agent.finish_source_chunks()
        session._audio_stream_active = False
        if hasattr(session, '_starting_audio_processor'):
            session._starting_audio_processor = False
        logger.info("🛑 AudioStream closed")


def whisper_ready_timeout():
    return min(1800, max(20, _env_int("WHISPER_MODEL_READY_TIMEOUT_S", 1800)))


def make_whisper_client(config):
    from asr.whisper_client import WhisperLiveClient
    return WhisperLiveClient(
        host=config.get("host", "localhost"), port=int(config.get("port", 9090)),
        language="ca", model=config.get("model", "small"), translate=False,
        use_vad=WHISPER_USE_VAD, no_speech_thresh=WHISPER_NO_SPEECH_THRESH,
        active_timeout_s=WHISPER_ACTIVE_TIMEOUT_S, clip_audio=WHISPER_CLIP_AUDIO,
        same_output_threshold=WHISPER_SAME_OUTPUT_THRESHOLD,
        vad_parameters=WHISPER_VAD_PARAMETERS,
        max_uncommitted_audio_s=WHISPER_MAX_UNCOMMITTED_AUDIO_S,
        context_overlap_s=WHISPER_CONTEXT_OVERLAP_S, latency_logging=LATENCY_LOGGING,
    )


async def prepare_whisper_client(config, event_id):
    """Reserve a ready ASR connection before creating a broadcast or its tracks."""
    await ensure_shared_model_admission(config)
    client = make_whisper_client(config)
    await broadcast_log(event_id, {"source": "whisper", "level": "info",
        "message": "Preparing ASR model. First use may download model files; wait for readiness."})
    try:
        await client.connect()
        await client.wait_until_ready(timeout=whisper_ready_timeout())
        return client
    except BaseException as error:
        await client.close()
        if isinstance(error, asyncio.CancelledError):
            raise
        busy = "busy" in str(error).lower() or "pending" in str(error).lower()
        raise HTTPException(409 if busy else 503,
            "ASR is busy or a model change is pending. Stop existing broadcasts and retry."
            if busy else "The ASR model could not be prepared. Retry Start or select another model.") from None


def active_asr_broadcasts(config, service_id):
    """Keep logical broadcasts protected even after an audio socket drains."""
    host, port = config.get("host", "").strip().lower(), str(config.get("port", 9090))
    return sum(1 for session in active_sessions.values() if session.is_active and (
        (service_id and session.asr_service_id == service_id) or
        ((session.whisper_config or {}).get("host", "").strip().lower() == host and
         str((session.whisper_config or {}).get("port", 9090)) == port)))


async def ensure_shared_model_admission(config):
    # Opt-in feature bundles require control status; legacy fixed-model stacks
    # retain their existing handshake without speculative control requests.
    if not _env_bool("WHISPER_MODEL_SELECTION_REQUIRED", False):
        return
    from asr.model_selection import model_request, ModelControlError
    if not SHARED_ASR_ENABLED:
        raise HTTPException(503, "Model selection requires the shared-ASR deployment profile.")
    try:
        state = await model_request(config)
    except ModelControlError as error:
        raise HTTPException(error.status, str(error)) from None
    if state.get("pending") and active_asr_broadcasts(config, state.get("service_id")):
        raise HTTPException(409, "A model change is pending. Stop existing event broadcasts before starting ASR again.")


def create_source_asr_agent(session: TranslationSession) -> WhisperAgent:
    """Create the single WhisperLive owner for source audio and original captions."""
    from translation.placeholder import NullTranslator

    whisper_cfg = session.whisper_config
    translator = NullTranslator(
        source_language=whisper_cfg.get("language", "ca"),
        target_language="original",
    )

    agent = WhisperAgent(
        target_language="original",
        event_id=session.event_id,
        audio_publisher=None,
        room=session.room,
        whisper_host=whisper_cfg.get("host", "localhost"),
        whisper_port=whisper_cfg.get("port", 9090),
        whisper_model=whisper_cfg.get("model", "small"),
        whisper_language=whisper_cfg.get("language", "ca"),
        source_language=session.source_language,
        is_primary_agent=True,
        translator=translator,
        owns_whisper=True,
    )
    agent.whisper = session.prepared_whisper
    session.prepared_whisper = None
    return agent


async def start_session_agents(session: TranslationSession):
    """Start shared-ASR workers or duplicated per-target ASR workers.

    NOTE: Preflight checks already validated that translator_url exists
    and all target languages are supported. No fallbacks needed.
    """
    from translation.aina_translator import AinaTranslator

    whisper_cfg = session.whisper_config
    translator_url = whisper_cfg.get("translator_url", "").strip()
    primary_assigned = False

    for lang in session.target_languages:
        if lang not in session.audio_publishers:
            continue
        should_be_active = session.source_language == "any" or lang != session.source_language

        # Create AINA Translator (preflight already validated URL + language)
        translator = AinaTranslator(
            base_url=translator_url,
            target_language=lang,
            source_language="ca",
        )
        logger.info(f"🌐 Agent {lang}: AINA Translator at {translator_url}")
        await broadcast_log(session.event_id, {
            "source": "translator",
            "level": "success",
            "message": f"AINA Translator → {lang.upper()} at {translator_url}"
        })

        agent = WhisperAgent(
            target_language=lang,
            event_id=session.event_id,
            audio_publisher=session.audio_publishers[lang],
            room=session.room,
            whisper_host=whisper_cfg.get("host", "localhost"),
            whisper_port=whisper_cfg.get("port", 9090),
            whisper_model=whisper_cfg.get("model", "small"),
            whisper_language=whisper_cfg.get("language", "ca"),
            source_language=session.source_language,
            is_primary_agent=(
                not SHARED_ASR_ENABLED and should_be_active and not primary_assigned
            ),
            translator=translator,
            owns_whisper=not SHARED_ASR_ENABLED,
        )
        if agent.is_primary_agent:
            primary_assigned = True
        session.whisper_agents[lang] = agent

        await agent.start()

        if should_be_active:
            if SHARED_ASR_ENABLED:
                await agent.start_source_chunk_consumer()
            else:
                await agent.connect_whisper()
        else:
            agent.is_active = False
            logger.info(f"⏸️ Agent {lang} starts in passthrough mode")

    if SHARED_ASR_ENABLED:
        source_agent = create_source_asr_agent(session)
        source_agent.fanout_agents = list(session.whisper_agents.values())
        session.source_asr_agent = source_agent

        logger.info(
            "ASR_MODE mode=shared connections=1 targets=%s experiment_id=%s run_id=%s repetition=%s",
            ",".join(session.whisper_agents.keys()) or "none",
            EXPERIMENT_ID or "none",
            EXPERIMENT_RUN_ID or "none",
            EXPERIMENT_REPETITION or "none",
        )
        await broadcast_log(session.event_id, {
            "source": "whisper",
            "level": "info",
            "message": "Starting shared ASR source agent (Whisper Live)"
        })

        await source_agent.start()
        await source_agent.connect_whisper()
    else:
        session.source_asr_agent = None
        connection_count = sum(
            1 for agent in session.whisper_agents.values()
            if agent.is_active and agent.owns_whisper
        )
        logger.info(
            "ASR_MODE mode=duplicated connections=%d targets=%s experiment_id=%s run_id=%s repetition=%s",
            connection_count,
            ",".join(session.whisper_agents.keys()) or "none",
            EXPERIMENT_ID or "none",
            EXPERIMENT_RUN_ID or "none",
            EXPERIMENT_REPETITION or "none",
        )
        await broadcast_log(session.event_id, {
            "source": "whisper",
            "level": "info",
            "message": f"Starting duplicated ASR ({connection_count} Whisper Live connections)"
        })


async def start_primary_transcription_agent(session: TranslationSession):
    """
    Start a dedicated primary agent for original transcription only.

    Used when there are NO translation channels but we still need to:
    - Connect to Whisper Live for ASR
    - Publish original subtitles via LiveKit data channel
    - Show transcriptions in the speaker terminal

    This agent has no TTS, no translator — just Whisper Live → subtitles.
    """
    agent = create_source_asr_agent(session)
    session.source_asr_agent = agent

    logger.info(f"📍 Primary original-only agent created (Whisper Live transcription)")
    await broadcast_log(session.event_id, {
        "source": "whisper",
        "level": "info",
        "message": "Starting original transcription agent (Whisper Live)"
    })

    # Start agent and connect to Whisper Live
    await agent.start()
    await agent.connect_whisper()


def needs_primary_transcription_agent(
    session: TranslationSession,
    whisper_config: Optional[dict],
) -> bool:
    """Use a dedicated original-only ASR agent only when no target owns ASR."""
    return bool(
        whisper_config
        and session.source_asr_agent is None
        and not session.whisper_agents
    )


async def change_session_source_language(session: TranslationSession, new_source: str):
    """
    Change source language for all agents in session.
    Tracks are NEVER deleted - only Whisper connections change.
    """
    logger.info(f"🔄 Changing source language: {session.source_language} → {new_source}")
    
    session.source_language = new_source
    if session.source_asr_agent:
        session.source_asr_agent.source_language = new_source
    
    active_agents = []
    passthrough_agents = []
    
    for lang, agent in session.whisper_agents.items():
        await agent.set_source_language(new_source)
        
        if agent.is_active:
            active_agents.append(lang)
        else:
            passthrough_agents.append(lang)
    
    logger.info(f"✅ Active: {active_agents}, Passthrough: {passthrough_agents}")
    
    return active_agents, passthrough_agents


async def stop_session_livekit(session: TranslationSession):
    """Stop all LiveKit components for session."""
    logger.info(f"🛑 Stopping session LiveKit components")
    
    # Mark tracks as inactive in database
    try:
        if supabase:
            supabase.table("livekit_tracks").update({"is_active": False}).eq("event_id", session.event_id).execute()
            logger.info(f"💾 Marked tracks as inactive for event {session.event_id}")
    except Exception as e:
        logger.warning(f"⚠️ Could not update tracks in DB: {e}")
    
    # Stop audio processor
    if session.audio_processor_task and not session.audio_processor_task.done():
        session.audio_processor_task.cancel()
    
    if session.prepared_whisper is not None:
        await session.prepared_whisper.close()
        session.prepared_whisper = None

    # Stop the shared ASR owner and translation/TTS workers.
    if session.source_asr_agent:
        await session.source_asr_agent.stop()
        session.source_asr_agent = None
    for agent in session.whisper_agents.values():
        await agent.stop()
    
    # Stop publishers
    for publisher in session.audio_publishers.values():
        await publisher.stop()
    
    # Disconnect room
    if session.room:
        await session.room.disconnect()
    
    session.is_active = False


# ============================================================================
# API Endpoints
# ============================================================================

@app.post("/api/session/create", response_model=CreateSessionResponse)
async def create_session(request: CreateSessionRequest):
    # Two clicks for the same event must not create two ASR reservations/rooms.
    lock = session_start_locks.setdefault(request.event_id, asyncio.Lock())
    async with lock:
        return await _create_session(request)


async def _create_session(request: CreateSessionRequest):
    """Create a new translation session for an event."""
    logger.info(f"📝 Creating session for event: {request.event_id} (source: {request.source_language})")
    
    if not LIVEKIT_READY:
        raise HTTPException(500, "LiveKit not configured. Check your .env file.")
    
    # Generate room name from event title
    room_name = sanitize_room_name(request.event_title)
    
    # Check if session already exists
    if request.event_id in active_sessions:
        session = active_sessions[request.event_id]
        if session.is_active:
            # Return existing session
            broadcaster_token = create_token(
                session.room_name,
                session.broadcaster_identity,
                name="Broadcaster",
                can_publish=True,
                can_subscribe=True,
            )
            # Calculate active agents
            active_agents = [lang for lang in session.target_languages 
                           if session.source_language == "any" or lang != session.source_language]
            return CreateSessionResponse(
                room_name=session.room_name,
                livekit_url=LIVEKIT_URL,
                broadcaster_token=broadcaster_token,
                session_id=request.event_id,
                active_agents=active_agents,
                success=True,
            )
    
    
    # Create broadcaster identity
    broadcaster_identity = f"broadcaster-{uuid.uuid4().hex[:8]}"
    
    # Extract target languages from channels (non-original channels)
    target_languages = []
    for channel in request.channels:
        if not channel.get("is_original", False):
            lang = channel.get("language", "en")
            if lang not in target_languages:
                target_languages.append(lang)
    
    # ================================================================
    # PREFLIGHT CHECKS — Validate Whisper/Translator only when needed
    # ================================================================
    needs_translation = bool(target_languages)

    # Load Whisper config whenever an event code is available so original-only
    # transcription can still run without translation channels.
    whisper_config = None
    if supabase and request.event_code:
        try:
            result = supabase.rpc("get_whisper_config", {"p_event_code": request.event_code}).execute()
            if result.data:
                whisper_config = result.data
        except Exception as e:
            logger.error(f"❌ Could not load whisper_config: {e}")

    whisper_host = ""
    whisper_port = 0
    if whisper_config:
        whisper_host = whisper_config.get("host", "").strip()
        whisper_port = int(whisper_config.get("port", 0) or 0)

    whisper_enabled = bool(whisper_host and whisper_port)

    preflight_errors = []

    if needs_translation:
        # Translation channels require Whisper host/port plus translator.
        if not whisper_config:
            preflight_errors.append("Whisper Live config not found. Go to Endpoints tab and configure Whisper host and port.")
        else:
            if not whisper_host:
                preflight_errors.append("Whisper Live host is empty. Go to Endpoints tab and set the Whisper host.")
            if not whisper_port:
                preflight_errors.append("Whisper Live port is empty. Go to Endpoints tab and set the Whisper port.")

            translator_url = whisper_config.get("translator_url", "").strip()
            if not translator_url:
                preflight_errors.append(
                    f"Translation channels ({', '.join(t.upper() for t in target_languages)}) require a Translator URL. "
                    f"Go to Endpoints tab and set the AINA Translator URL."
                )
            else:
                from translation.aina_translator import SUPPORTED_TARGETS
                unsupported = [l for l in target_languages if l not in SUPPORTED_TARGETS]
                if unsupported:
                    preflight_errors.append(
                        f"Languages not supported by AINA Translator: {', '.join(l.upper() for l in unsupported)}. "
                        f"Supported: {', '.join(s.upper() for s in sorted(SUPPORTED_TARGETS))}."
                    )

        if preflight_errors:
            error_msg = "PREFLIGHT FAILED — Cannot start streaming:\n" + "\n".join(f"  • {e}" for e in preflight_errors)
            logger.error(f"🚫 {error_msg}")

            await broadcast_log(request.event_id, {
                "source": "system",
                "level": "error",
                "message": "🚫 PREFLIGHT FAILED — Cannot start streaming"
            })
            for err in preflight_errors:
                await broadcast_log(request.event_id, {
                    "source": "system",
                    "level": "error",
                    "message": f"❌ {err}"
                })

            raise HTTPException(
                status_code=400,
                detail=f"Preflight failed: {'; '.join(preflight_errors)}"
            )

    if whisper_enabled:
        # Test Whisper Live connection whenever we intend to run ASR, including
        # original-only transcription with no translation channels.
        await broadcast_log(request.event_id, {
            "source": "whisper",
            "level": "info",
            "message": f"Testing Whisper Live connection ({whisper_host}:{whisper_port})..."
        })

        try:
            import websockets as ws_lib
            test_ws = await asyncio.wait_for(
                ws_lib.connect(f"ws://{whisper_host}:{whisper_port}", ping_interval=None),
                timeout=5.0
            )
            await test_ws.close()
            logger.info(f"✅ Whisper Live reachable at {whisper_host}:{whisper_port}")
            await broadcast_log(request.event_id, {
                "source": "whisper",
                "level": "success",
                "message": f"Whisper Live reachable at {whisper_host}:{whisper_port}"
            })
        except Exception as e:
            error_msg = f"Cannot connect to Whisper Live at {whisper_host}:{whisper_port} — {e}"
            logger.error(f"🚫 {error_msg}")
            await broadcast_log(request.event_id, {
                "source": "whisper",
                "level": "error",
                "message": f"🚫 {error_msg}"
            })
            raise HTTPException(status_code=400, detail=f"Preflight failed: {error_msg}")

    if needs_translation:
        # Test translator reachability.
        translator_url = whisper_config.get("translator_url", "").strip()

        await broadcast_log(request.event_id, {
            "source": "translator",
            "level": "info",
            "message": f"Testing AINA Translator ({translator_url})..."
        })

        try:
            from translation.aina_translator import AinaTranslator
            test_translator = AinaTranslator(
                base_url=translator_url,
                target_language=target_languages[0],
                source_language="ca",
                timeout=10.0,
            )
            health = await test_translator.health_check()
            await test_translator.close()

            if health.get("status") == "error":
                raise Exception(health.get("error", "Unknown error"))

            logger.info(f"✅ AINA Translator reachable: {health}")
            await broadcast_log(request.event_id, {
                "source": "translator",
                "level": "success",
                "message": f"AINA Translator online — device: {health.get('device', '?')}, models: {health.get('models_loaded', '?')}"
            })
        except Exception as e:
            error_msg = f"Cannot connect to AINA Translator at {translator_url} — {e}"
            logger.error(f"🚫 {error_msg}")
            await broadcast_log(request.event_id, {
                "source": "translator",
                "level": "error",
                "message": f"🚫 {error_msg}"
            })
            raise HTTPException(status_code=400, detail=f"Preflight failed: {error_msg}")
    elif not whisper_enabled:
        logger.info("✅ Audio-only session: skipping Whisper/Translator preflight")
        await broadcast_log(request.event_id, {
            "source": "system",
            "level": "info",
            "message": "Audio-only session: original speaker audio will be published without ASR"
        })

    # ================================================================
    # PREFLIGHT PASSED — All systems go
    # ================================================================
    logger.info(f"✅ Preflight checks passed — starting session")
    await broadcast_log(request.event_id, {
        "source": "system",
        "level": "success",
        "message": "✅ Preflight checks passed — all systems go"
    })

    prepared_whisper = None
    if whisper_enabled and (SHARED_ASR_ENABLED or not target_languages):
        prepared_whisper = await prepare_whisper_client(whisper_config, request.event_id)
    try:
        await ensure_room_exists(room_name)
    except BaseException:
        if prepared_whisper is not None:
            await prepared_whisper.close()
        raise

    # Create session
    session = TranslationSession(
        event_id=request.event_id,
        room_name=room_name,
        source_language=request.source_language,
        target_languages=target_languages,
        broadcaster_identity=broadcaster_identity,
        is_active=True,
        whisper_config=whisper_config,
        prepared_whisper=prepared_whisper,
        asr_service_id=getattr(prepared_whisper, "model_service_id", None),
    )

    # Store session BEFORE setting up LiveKit
    active_sessions[request.event_id] = session

    # Store event_id -> event_code mapping for WebSocket broadcasts
    if request.event_code:
        event_id_to_code[request.event_id] = request.event_code

    # Calculate which agents should be active
    active_agents = []
    passthrough_agents = []

    logger.info(f"🤖 Setting up session (source: {request.source_language})...")
    await broadcast_log(request.event_id, {
        "source": "system",
        "level": "info",
        "message": f"Setting up session (source: {request.source_language})"
    })

    try:
        # Setup LiveKit connection and create tracks for translation channels
        await setup_session_livekit(session)

        # Start Whisper agents for translation channels
        if target_languages:
            await start_session_agents(session)

            # Determine active vs passthrough
            for lang in target_languages:
                if request.source_language != "any" and lang == request.source_language:
                    passthrough_agents.append(lang)
                else:
                    active_agents.append(lang)

            logger.info(f"   Active: {active_agents}, Passthrough: {passthrough_agents}")

        # If no translation channels, start original transcription only when Whisper is configured.
        if needs_primary_transcription_agent(session, whisper_config):
            await start_primary_transcription_agent(session)
            active_agents.append("original")
        elif not session.source_asr_agent and not session.whisper_agents:
            logger.info("📍 No ASR configured: running original-audio-only session")
            await broadcast_log(request.event_id, {
                "source": "system",
                "level": "info",
                "message": "Original audio passthrough active. No ASR/transcription agent configured."
            })
    
        # Create broadcaster token
        broadcaster_token = create_token(
            room_name,
            broadcaster_identity,
            name="Broadcaster",
            can_publish=True,
            can_subscribe=True,
        )
    
    except BaseException as error:
        try:
            await stop_session_livekit(session)
        finally:
            active_sessions.pop(request.event_id, None)
        if isinstance(error, asyncio.CancelledError):
            raise
        raise HTTPException(503, "Broadcast setup failed. No audio was started; retry after checking ASR readiness.") from None

    # UPDATE EVENT AND CHANNELS in Supabase
    # This triggers Supabase Realtime notification to Stage app
    try:
        if supabase:
            # Update event status
            supabase.table("events").update({
                "status": "streaming",
                "livekit_room_name": room_name
            }).eq("id", request.event_id).execute()
            logger.info(f"📡 Event status updated to 'streaming', room={room_name}")
            
            # GET ALL CHANNELS FROM SUPABASE (not from request!)
            # This ensures we update ALL channels, not just what frontend sent
            channels_response = supabase.table("channels").select("*").eq("event_id", request.event_id).execute()
            db_channels = channels_response.data if channels_response.data else []
            
            logger.info(f"📡 Found {len(db_channels)} channels in database for event {request.event_id}")
            
            # UPDATE EACH CHANNEL with LiveKit info
            for channel in db_channels:
                channel_id = channel.get("id")
                lang = channel.get("language", "unknown")
                is_original = channel.get("is_original", False)
                
                # Track name depends on whether it's original or translation
                if is_original:
                    track_name = "broadcaster_audio"
                else:
                    track_name = f"tts-{lang}"
                
                # Update channel BY ID (not by language - could have duplicates)
                update_result = supabase.table("channels").update({
                    "status": "active",
                    "livekit_room_name": room_name,
                    "livekit_url": LIVEKIT_URL,
                    "track_name": track_name
                }).eq("id", channel_id).execute()
                
                logger.info(f"📡 Channel updated: id={channel_id}, lang={lang}, room={room_name}, track={track_name}")
            
            logger.info(f"✅ All {len(db_channels)} channels updated with LiveKit info")
    except Exception as e:
        logger.error(f"❌ Could not update event/channels status: {e}")
        import traceback
        traceback.print_exc()
    except Exception as e:
        logger.warning(f"⚠️ Could not update event/channels status: {e}")
    
    logger.info(f"✅ Session created: {room_name}, active: {active_agents}, passthrough: {passthrough_agents}")
    
    return CreateSessionResponse(
        room_name=room_name,
        livekit_url=LIVEKIT_URL,
        broadcaster_token=broadcaster_token,
        session_id=request.event_id,
        active_agents=active_agents,
        success=True,
    )


async def remove_stale_stage_listeners(room_name: str, current_identity: str):
    """Remove old browser listeners so stale tabs cannot keep playing audio."""
    if not LIVEKIT_READY:
        return

    from livekit import api

    lkapi = await get_livekit_api()
    try:
        participants = await lkapi.room.list_participants(api.ListParticipantsRequest(room=room_name))
        for participant in participants.participants:
            identity = participant.identity or ""
            if identity == current_identity:
                continue
            if identity.startswith("stage-") or identity.startswith("listener-"):
                try:
                    await lkapi.room.remove_participant(
                        api.RoomParticipantIdentity(room=room_name, identity=identity)
                    )
                    logger.info("🔇 Removed stale stage listener from room %s: %s", room_name, identity)
                except Exception as e:
                    logger.warning("⚠️ Could not remove stale listener %s: %s", identity, e)
    except Exception as e:
        logger.warning("⚠️ Could not list LiveKit participants for %s: %s", room_name, e)
    finally:
        await lkapi.aclose()


@app.post("/api/session/{event_id}/join", response_model=JoinSessionResponse)
async def join_session(event_id: str, request: JoinSessionRequest):
    """Join an existing session as listener."""
    if event_id not in active_sessions:
        raise HTTPException(404, "Session not found")
    
    session = active_sessions[event_id]
    
    if request.role == "broadcaster":
        token = create_token(session.room_name, request.identity, can_publish=True, can_subscribe=True)
    else:
        token = create_token(session.room_name, request.identity, can_publish=False, can_subscribe=True)
    
    return JoinSessionResponse(
        token=token,
        livekit_url=LIVEKIT_URL,
        room_name=session.room_name,
    )


@app.post("/api/session/{event_id}/source-language")
async def change_source_language(event_id: str, request: SourceLanguageRequest):
    """
    Change the source language for an active session.
    
    IMPORTANT: 
    - Tracks are NEVER deleted (created once at session start)
    - Only Whisper Live connections change
    - Listeners stay connected without any interruption
    
    When source == target: that channel plays original audio (passthrough)
    When source != target: that channel plays translated TTS
    """
    if event_id not in active_sessions:
        raise HTTPException(404, "Session not found")
    
    session = active_sessions[event_id]
    old_source = session.source_language
    new_source = request.source_language
    
    logger.info(f"🔄 Changing source language: {old_source} → {new_source}")
    
    await broadcast_log(event_id, {
        "source": "system",
        "level": "info",
        "message": f"Changing source: {old_source} → {new_source}"
    })
    
    # Use the session manager function to change all agents
    active_agents, passthrough_agents = await change_session_source_language(session, new_source)
    
    logger.info(f"✅ Source changed: Active={active_agents}, Passthrough={passthrough_agents}")
    
    await broadcast_log(event_id, {
        "source": "system",
        "level": "success",
        "message": f"Active: {', '.join(active_agents) or 'none'} | Passthrough: {', '.join(passthrough_agents) or 'none'}"
    })
    
    return {
        "status": "ok",
        "source_language": new_source,
        "active_agents": active_agents,
        "passthrough_agents": passthrough_agents
    }


@app.delete("/api/session/{event_id}")
async def stop_session(event_id: str):
    # Stop cannot tear down a half-created session while Start is still
    # handing the reserved model connection to its source agent.
    lock = session_start_locks.setdefault(event_id, asyncio.Lock())
    async with lock:
        return await _stop_session(event_id)


async def _stop_session(event_id: str):
    """Stop a translation session."""
    if event_id not in active_sessions:
        raise HTTPException(404, "Session not found")
    
    session = active_sessions[event_id]
    
    # Stop all LiveKit components using session manager
    await stop_session_livekit(session)
    
    # Delete room from LiveKit
    if LIVEKIT_READY:
        from livekit import api
        lkapi = await get_livekit_api()
        try:
            await lkapi.room.delete_room(api.DeleteRoomRequest(room=session.room_name))
        except:
            pass
        finally:
            await lkapi.aclose()
    
    # UPDATE EVENT STATUS BACK TO "active" in Supabase
    # This triggers Supabase Realtime notification to Stage app
    try:
        if supabase:
            # Update event status
            supabase.table("events").update({
                "status": "active",
                "livekit_room_name": None
            }).eq("id", event_id).execute()
            logger.info(f"📡 Event status updated to 'active'")
            
            # UPDATE ALL CHANNELS - clear LiveKit info
            supabase.table("channels").update({
                "status": "inactive",
                "livekit_room_name": None,
                "livekit_url": None,
                "track_name": None
            }).eq("event_id", event_id).execute()
            logger.info(f"📡 Channels cleared and set to 'inactive'")
    except Exception as e:
        logger.warning(f"⚠️ Could not update event/channels status: {e}")
    
    del active_sessions[event_id]
    
    return {"status": "stopped"}


@app.get("/api/session/{event_id}/status")
async def get_session_status(event_id: str):
    """Get session status."""
    if event_id not in active_sessions:
        return {"active": False}
    
    session = active_sessions[event_id]
    
    # Get agent status
    active_agents = []
    passthrough_agents = []
    for lang, agent in session.whisper_agents.items():
        if agent.is_active:
            active_agents.append(lang)
        else:
            passthrough_agents.append(lang)
    if session.source_asr_agent and session.source_asr_agent.is_active:
        active_agents.append("original")
    owners = ([session.source_asr_agent] if session.source_asr_agent else
              [agent for agent in session.whisper_agents.values() if getattr(agent, "owns_whisper", False)])
    errors = [agent.asr_error for agent in owners if getattr(agent, "asr_error", None)]
    
    return {
        "active": session.is_active,
        "room_name": session.room_name,
        "source_language": session.source_language,
        "target_languages": session.target_languages,
        "active_agents": active_agents,
        "passthrough_agents": passthrough_agents,
        "asr_status": "failed" if errors else "running" if owners else "not_required",
        "asr_error": errors[0] if errors else None,
    }


@app.get("/api/livekit/status")
async def get_livekit_status():
    """Check if LiveKit is configured and ready."""
    return {
        "configured": LIVEKIT_READY,
        "translation_ready": TRANSLATION_READY,
        "livekit_url": LIVEKIT_URL if LIVEKIT_READY else None,
        "whisper_live": True,  # Self-hosted, always available
        "google_configured": bool(GOOGLE_CREDS),
    }


# ============================================================================
# WebSocket for Logs
# ============================================================================

@app.websocket("/ws/logs/{event_id}")
async def websocket_logs(websocket: WebSocket, event_id: str):
    """WebSocket endpoint for real-time logs."""
    await websocket.accept()
    
    if event_id not in log_connections:
        log_connections[event_id] = []
    log_connections[event_id].append(websocket)
    
    try:
        while True:
            # Keep connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        log_connections[event_id].remove(websocket)


async def broadcast_log(event_id: str, message: dict):
    """Broadcast log message to all connected clients."""
    if event_id in log_connections:
        for ws in log_connections[event_id]:
            try:
                await ws.send_json(message)
            except:
                pass


# ============================================================================
# Whisper Live Endpoint Configuration
# ============================================================================

@app.get("/api/event/{event_code}/endpoints")
async def get_event_endpoints(event_code: str):
    """Get Whisper Live endpoint configuration for an event."""
    if not supabase:
        raise HTTPException(500, f"Database not configured (key_type={_supabase_key_type}, env_exists={(BASE_DIR / '.env').exists()})")

    try:
        # Use RPC to bypass PostgREST schema cache issues
        result = supabase.rpc("get_whisper_config", {"p_event_code": event_code}).execute()

        if result.data is not None:
            config = result.data or {"host": "", "port": 9090, "model": "small", "language": "ca", "translator_url": ""}
            return config

        logger.error(f"❌ Event {event_code} not found via RPC.")
        raise HTTPException(404, f"Event {event_code} not found.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting endpoints: {e}")
        raise HTTPException(500, str(e))


@app.post("/api/event/{event_code}/endpoints")
async def save_event_endpoints(event_code: str, config: dict = Body(...)):
    """Save Whisper Live endpoint configuration for an event."""
    if not supabase:
        raise HTTPException(500, f"Database not configured (key_type={_supabase_key_type})")

    # The new shared model has its own Save action. Preserve the legacy event
    # field when saving host/port/translator settings; do not reset it to small.
    existing = await get_event_endpoints(event_code) if "model" not in config else {}
    whisper_config = {
        "host": config.get("host", ""),
        "port": int(config.get("port", 9090)),
        "model": config.get("model", existing.get("model", "small")),
        "language": config.get("language", "ca"),
        "translator_url": config.get("translator_url", ""),
    }

    try:
        # Use RPC to bypass PostgREST schema cache issues
        result = supabase.rpc("set_whisper_config", {
            "p_event_code": event_code,
            "p_config": whisper_config
        }).execute()

        if result.data is not None:
            logger.info(f"📡 Saved whisper config for {event_code}: {whisper_config['host']}:{whisper_config['port']}")
            return {"success": True, "config": whisper_config}

        logger.error(f"❌ Update returned no data for {event_code}.")
        raise HTTPException(500, f"Update failed for event {event_code}.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving endpoints: {e}")
        raise HTTPException(500, str(e))


# ============================================================================
# Public Captions API & WebSocket
# ============================================================================

async def event_asr_model_request(event_code, operation="status", model=None, revision=None):
    from asr.model_selection import model_request, ModelControlError
    if not _env_bool("WHISPER_MODEL_SELECTION_REQUIRED", False):
        raise HTTPException(503, "Shared model selection is not enabled in this deployment profile.")
    try:
        config = await get_event_endpoints(event_code)
    except HTTPException as error:
        raise HTTPException(error.status_code, "Could not read this event's saved ASR endpoint configuration.") from None
    try:
        state = await model_request(config, operation, model=model, revision=revision)
    except ModelControlError as error:
        raise HTTPException(error.status, str(error)) from None
    return {**state, "active_broadcasts": active_asr_broadcasts(config, state.get("service_id")),
            "endpoint": {"host": config.get("host", ""), "port": config.get("port", 9090)}}


@app.get("/api/event/{event_code}/asr-model")
async def get_shared_asr_model(event_code: str):
    return await event_asr_model_request(event_code)


@app.post("/api/event/{event_code}/asr-model")
async def save_shared_asr_model(event_code: str, selection: dict = Body(...)):
    return await event_asr_model_request(event_code, "select", selection.get("model"),
                                         selection.get("selection_revision"))

@app.get("/api/public/event/{event_code}")
async def get_public_event(event_code: str):
    """
    Public endpoint to get event info and channels.
    No authentication required.
    Uses Supabase REST API directly with service key (bypasses RLS).
    """
    import httpx
    
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    
    if not supabase_url:
        logger.error("❌ SUPABASE_URL not configured in .env")
        raise HTTPException(500, "SUPABASE_URL not configured")
    
    if not supabase_key:
        logger.error("❌ SUPABASE_SERVICE_KEY not configured in .env")
        raise HTTPException(500, "SUPABASE_SERVICE_KEY not configured - get it from Supabase Dashboard > Project Settings > API > service_role")
    
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json"
    }
    
    try:
        async with httpx.AsyncClient() as client:
            # Get event (service key bypasses RLS, so we get all events)
            event_url = f"{supabase_url}/rest/v1/events?event_code=eq.{event_code}&select=id,title,event_code,status"
            logger.info(f"📡 Fetching event: {event_code}")
            event_response = await client.get(event_url, headers=headers)
            
            if event_response.status_code != 200:
                logger.error(f"❌ Supabase error: {event_response.status_code} - {event_response.text}")
                raise HTTPException(500, f"Database error: {event_response.status_code}")
            
            events = event_response.json()
            logger.info(f"📡 Found {len(events)} events")
            
            if not events or len(events) == 0:
                logger.warning(f"⚠️ Event not found: {event_code}")
                raise HTTPException(404, f"Event '{event_code}' not found")
            
            event = events[0]
            logger.info(f"✅ Event: {event['title']} (status: {event['status']})")
            
            # Get channels
            channels_url = f"{supabase_url}/rest/v1/channels?event_id=eq.{event['id']}&select=id,name,language,is_original"
            channels_response = await client.get(channels_url, headers=headers)
            
            channels = []
            if channels_response.status_code == 200:
                channels = channels_response.json()
                logger.info(f"✅ Channels: {[c['language'] for c in channels]}")
            
            return {
                "event": {
                    "id": event['id'],
                    "title": event['title'],
                    "code": event['event_code'],
                    "status": event['status']
                },
                "channels": channels
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Public event API error: {e}")
        raise HTTPException(500, str(e))


@app.get("/api/public/event/{event_code}/tracks")
async def get_public_event_tracks(event_code: str):
    """
    Get active LiveKit tracks for an event.
    
    Public endpoint for external services to discover available tracks.
    Returns track info needed to connect (room_name, livekit_url, track_name, language).
    
    To connect and listen to a track:
    1. Call this endpoint to get track info
    2. Call /api/session/{event_id}/join with identity and language to get a token
    3. Connect to LiveKit with that token
    """
    import httpx
    
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    
    if not supabase_url or not supabase_key:
        raise HTTPException(500, "Supabase not configured")
    
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json"
    }
    
    try:
        async with httpx.AsyncClient() as client:
            # Get event
            event_url = f"{supabase_url}/rest/v1/events?event_code=eq.{event_code}&select=id,title,event_code,status"
            event_response = await client.get(event_url, headers=headers)
            
            if event_response.status_code != 200:
                raise HTTPException(500, "Database error")
            
            events = event_response.json()
            if not events:
                raise HTTPException(404, f"Event '{event_code}' not found")
            
            event = events[0]
            
            # Only return tracks for active/streaming events
            if event['status'] not in ('active', 'streaming'):
                return {"event": event, "tracks": [], "message": "Event is not currently streaming"}
            
            # Get active tracks
            tracks_url = f"{supabase_url}/rest/v1/livekit_tracks?event_id=eq.{event['id']}&is_active=eq.true&select=*"
            tracks_response = await client.get(tracks_url, headers=headers)
            
            tracks = []
            if tracks_response.status_code == 200:
                tracks = tracks_response.json()
            
            return {
                "event": {
                    "id": event['id'],
                    "title": event['title'],
                    "code": event['event_code'],
                    "status": event['status']
                },
                "tracks": [
                    {
                        "track_name": t['track_name'],
                        "language": t['language'],
                        "room_name": t['room_name'],
                        "livekit_url": t['livekit_url'],
                        "is_active": t['is_active']
                    }
                    for t in tracks
                ],
                "how_to_connect": "Use /api/session/{event_id}/join with identity and language to get a connection token"
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Public tracks API error: {e}")
        raise HTTPException(500, str(e))


@app.websocket("/ws/captions/{event_code}/{channel}")
async def websocket_captions(websocket: WebSocket, event_code: str, channel: str):
    """
    WebSocket endpoint for receiving captions.
    Public - no authentication required.
    Clients connect and wait for captions to be broadcast.
    """
    await captions_manager.connect(websocket, event_code, channel)
    
    try:
        while True:
            # Keep connection alive, ignore any incoming messages
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                # Send ping to keep alive
                try:
                    await websocket.send_json({"type": "ping"})
                except:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"Captions WS error: {e}")
    finally:
        captions_manager.disconnect(websocket, event_code, channel)


async def broadcast_caption(event_code: str, channel: str, data: dict):
    """Broadcast caption data to all connected WebSocket clients."""
    message = json.dumps(data)
    await captions_manager.broadcast(event_code, channel, message)


# ============================================================================
# Health Check (useful for debugging production deployments)
# ============================================================================

@app.get("/api/health")
async def health_check():
    """Check server configuration status. Useful for debugging deployment issues."""
    # Quick RPC test to verify Supabase actually works
    rpc_test = "not_tested"
    if supabase:
        try:
            r = supabase.rpc("get_whisper_config", {"p_event_code": "A46326"}).execute()
            rpc_test = "ok" if r.data is not None else "no_data"
        except Exception as e:
            rpc_test = f"error: {e}"

    return {
        "status": "ok",
        "supabase_configured": supabase is not None,
        "supabase_key_type": _supabase_key_type,
        "supabase_url": SUPABASE_URL,
        "supabase_service_key_set": bool(SUPABASE_SERVICE_KEY),
        "supabase_service_key_preview": SUPABASE_SERVICE_KEY[:20] + "..." if SUPABASE_SERVICE_KEY else "(empty)",
        "supabase_anon_key_set": bool(SUPABASE_ANON_KEY),
        "livekit_configured": LIVEKIT_READY,
        "app_url": APP_URL,
        "env_file": str(BASE_DIR / ".env"),
        "env_file_exists": (BASE_DIR / ".env").exists(),
        "rpc_test": rpc_test,
    }


# ============================================================================
# Page Routes
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY, "APP_URL": APP_URL},
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY, "APP_URL": APP_URL},
    )


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    return templates.TemplateResponse(
        request,
        "signup.html",
        {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY, "APP_URL": APP_URL},
    )


@app.get("/pending-approval", response_class=HTMLResponse)
async def pending_page(request: Request):
    return templates.TemplateResponse(
        request,
        "pending-approval.html",
        {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY, "APP_URL": APP_URL},
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY, "APP_URL": APP_URL},
    )


@app.get("/event/{event_code}", response_class=HTMLResponse)
async def event_page(request: Request, event_code: str):
    return templates.TemplateResponse(
        request,
        "event.html",
        {"event_code": event_code, "SUPABASE_URL": SUPABASE_URL, "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY, "APP_URL": APP_URL},
    )


@app.get("/speaker/{event_code}", response_class=HTMLResponse)
async def speaker_page(request: Request, event_code: str):
    return templates.TemplateResponse(
        request,
        "speaker.html",
        {"event_code": event_code, "SUPABASE_URL": SUPABASE_URL, "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY, "APP_URL": APP_URL},
    )


@app.get("/monitor/{event_code}", response_class=HTMLResponse)
async def monitor_page(request: Request, event_code: str):
    """Private monitor page with full logs and track selection."""
    return templates.TemplateResponse(
        request,
        "monitor.html",
        {"event_code": event_code, "SUPABASE_URL": SUPABASE_URL, "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY, "APP_URL": APP_URL},
    )


@app.get("/archive", response_class=HTMLResponse)
async def archive_page(request: Request):
    return templates.TemplateResponse(
        request,
        "archive.html",
        {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY, "APP_URL": APP_URL},
    )


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request):
    return templates.TemplateResponse(
        request,
        "account.html",
        {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY, "APP_URL": APP_URL},
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY, "APP_URL": APP_URL},
    )


@app.get("/stage/{event_code}", response_class=HTMLResponse)
async def stage_page(request: Request, event_code: str):
    """Public listener page."""
    return templates.TemplateResponse(
        request,
        "stage.html",
        {"event_code": event_code, "SUPABASE_URL": SUPABASE_URL, "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY, "APP_URL": APP_URL},
    )


@app.get("/event/{event_code}/publicdomain/captions", response_class=HTMLResponse)
async def captions_page(request: Request, event_code: str):
    """Public domain captions overlay page for HDMI capture."""
    return templates.TemplateResponse(
        request,
        "captions.html",
        {"event_code": event_code, "SUPABASE_URL": SUPABASE_URL, "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY, "APP_URL": APP_URL},
    )


# Static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ============================================================================
# Run
# ============================================================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
