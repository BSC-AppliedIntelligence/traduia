"""
Cliente WebSocket async para Whisper Live.

Implementa el protocolo WebSocket de whisper-live directamente,
aceptando audio PCM 16kHz desde LiveKit (NO captura micrófono).

CARACTERÍSTICAS:
- Emisión INMEDIATA de tokens parciales (para display en UI)
- Chunks completos para TTS (al llegar puntuación O 30+ palabras)
- Cortafuegos de latencia: si hay 30+ palabras sin puntuación, emite igualmente
- Compatible con la interfaz existente de subtítulos
"""
import asyncio
import json
import uuid
import logging
import re
import time
from typing import Any, AsyncIterator, Optional
from dataclasses import dataclass

import numpy as np
import websockets

logger = logging.getLogger(__name__)

# Cortafuegos de latencia: máximo palabras antes de forzar emisión
MAX_WORDS_BEFORE_FLUSH = 30

# Puntuación
CHUNK_PUNCTUATION = {',', '.', '?', '!', ';', ':', '。', '?', '!'}
SENTENCE_END = {'.', '?', '!', '。', '?', '!'}


ASR_ERROR_MESSAGES = {
    "prefix_context_lost": "Transcription stopped because retained speech context could not be recovered safely. Stop and restart the session.",
    "asr_disconnected": "Transcription disconnected before the session ended. Stop and restart the session.",
    "asr_protocol_error": "Transcription stopped because the ASR stream was invalid. Stop and restart the session.",
    "asr_stream_failed": "Transcription stopped while processing audio. Stop and restart the session.",
    "model_busy": "ASR is busy or a model change is pending. Wait for existing broadcasts to finish.",
    "model_load_failed": "The selected ASR model could not be prepared. Retry or select another model.",
}


class ASRStreamError(ConnectionError):
    def __init__(self, code):
        self.code = code if code in ASR_ERROR_MESSAGES else "asr_stream_failed"
        super().__init__(ASR_ERROR_MESSAGES[self.code])


@dataclass
class TranscriptionChunk:
    """
    Un chunk de transcripción de Whisper Live.

    Tipos:
    - is_partial=True: Texto parcial para mostrar en UI inmediatamente (creciente)
    - is_partial=False: Chunk completo para TTS (puntuación o 30+ palabras)
    """
    text: str                          # El texto transcrito
    language: Optional[str] = None     # Idioma detectado
    is_partial: bool = False           # True = parcial para UI, False = chunk para TTS
    punctuation: str = ""              # La puntuación que terminó el chunk
    is_sentence_end: bool = False      # True si es fin de oración (. ? !)
    start_time: float = 0.0           # Timestamp de inicio del segmento
    end_time: float = 0.0            # Timestamp de fin del segmento
    latency_ms: Optional[float] = None # Fin de audio estimado -> final disponible
    finalization_reason: str = ""     # stable, timeout, bounded_window o eos
    chunk_id: str = ""                # Identificador para correlacionar logs
    prefix_protocol: bool = False     # Ordered text IDs; acoustic times unavailable.


class WhisperLiveClient:
    """
    Cliente async para Whisper Live server via WebSocket.

    NO usa micrófono - recibe audio vía send_audio().

    Protocolo Whisper Live:
    1. Conectar a ws://{host}:{port}
    2. Enviar config JSON: {uid, language, model, task, use_vad}
    3. Enviar audio como bytes PCM 16kHz mono en chunks
    4. Recibir JSON con segments: [{text, start, end}]
    """

    def __init__(
        self,
        host: str,
        port: int,
        language: str = "ca",
        model: str = "small",
        translate: bool = False,
        use_vad: bool = True,
        no_speech_thresh: float = 0.45,
        active_timeout_s: float = 2.0,
        clip_audio: bool = False,
        same_output_threshold: Optional[int] = None,
        vad_parameters: Optional[dict[str, Any]] = None,
        max_uncommitted_audio_s: float = 0.0,
        context_overlap_s: float = 1.0,
        latency_logging: bool = False,
    ):
        self.host = host
        self.port = port
        self.language = language
        self.model = model
        self.task = "translate" if translate else "transcribe"
        self.use_vad = use_vad
        self.no_speech_thresh = no_speech_thresh
        self.active_timeout_s = active_timeout_s
        self.clip_audio = clip_audio
        self.same_output_threshold = same_output_threshold
        self.vad_parameters = vad_parameters
        self.max_uncommitted_audio_s = max(0.0, float(max_uncommitted_audio_s or 0.0))
        self.context_overlap_s = max(0.0, float(context_overlap_s or 0.0))
        self.latency_logging = latency_logging
        self.uid = str(uuid.uuid4())

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._connected = False
        self._server_ready = False
        self._server_info: dict[str, Any] = {}
        self._server_error: Optional[str] = None
        self._server_error_code = ""
        self._closing = False
        self.model_service_id: Optional[str] = None
        self._receive_queue: asyncio.Queue = asyncio.Queue()
        self._receiver_task: Optional[asyncio.Task] = None
        self._audio_started_at: Optional[float] = None
        self._audio_samples_sent = 0
        self._final_chunk_sequence = 0

    @staticmethod
    def _segment_time(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _final_metadata(self, end_time: float) -> tuple[Optional[float], str]:
        self._final_chunk_sequence += 1
        chunk_id = f"{self.uid[:8]}-{self._final_chunk_sequence}"
        if self._audio_started_at is None or end_time <= 0:
            return None, chunk_id

        audio_sent_duration = self._audio_samples_sent / 16000.0
        if end_time > audio_sent_duration + 0.25:
            logger.warning(
                "Cannot estimate ASR latency: segment end %.3fs exceeds sent audio %.3fs",
                end_time,
                audio_sent_duration,
            )
            return None, chunk_id

        segment_ended_at = self._audio_started_at + end_time
        latency_ms = max(0.0, (time.monotonic() - segment_ended_at) * 1000.0)
        return latency_ms, chunk_id

    @staticmethod
    def _final_key(text: str) -> str:
        normalized = re.sub(r"[^\w\s]", " ", text.casefold())
        return " ".join(normalized.split())

    @staticmethod
    def _time_overlap_ratio(
        start_a: float,
        end_a: float,
        start_b: float,
        end_b: float,
    ) -> float:
        duration_a = max(0.0, end_a - start_a)
        duration_b = max(0.0, end_b - start_b)
        if duration_a <= 0.0 or duration_b <= 0.0:
            return 0.0
        overlap = min(end_a, end_b) - max(start_a, start_b)
        if overlap <= 0.0:
            return 0.0
        return overlap / min(duration_a, duration_b)

    @classmethod
    def _is_similar_revision(
        cls,
        previous_text: str,
        current_text: str,
        previous_start: float,
        previous_end: float,
        current_start: float,
        current_end: float,
    ) -> bool:
        overlap_ratio = cls._time_overlap_ratio(
            previous_start,
            previous_end,
            current_start,
            current_end,
        )
        if overlap_ratio < 0.65:
            return False

        previous_key = cls._final_key(previous_text)
        current_key = cls._final_key(current_text)
        if not previous_key or not current_key:
            return False

        # A timeout final is still a hypothesis. A later result may extend it
        # or correct even one meaningful word (for example, a negation).
        # Containment and fuzzy similarity must not discard those revisions:
        # the caption/translation layer already knows how to replace a draft.
        # Only case/punctuation/whitespace variants are duplicates here.
        return previous_key == current_key

    @classmethod
    def _final_already_emitted(
        cls,
        emitted_final_ranges: dict[str, list[tuple[float, float]]],
        emitted_final_records: list[tuple[str, float, float]],
        text: str,
        start_time: float,
        end_time: float,
    ) -> bool:
        text_key = cls._final_key(text)
        if not text_key:
            return False

        for emitted_start, emitted_end in emitted_final_ranges.get(text_key, []):
            overlap = min(end_time, emitted_end) - max(start_time, emitted_start)
            if overlap > 0.0:
                return True

            # WhisperLive may report tiny boundary shifts when confirming a
            # timed-out hypothesis as stable.
            if abs(start_time - emitted_start) <= 0.75 and abs(end_time - emitted_end) <= 0.75:
                return True

        for emitted_text, emitted_start, emitted_end in emitted_final_records:
            if cls._is_similar_revision(
                emitted_text,
                text,
                emitted_start,
                emitted_end,
                start_time,
                end_time,
            ):
                return True

        return False

    @classmethod
    def _mark_final_emitted(
        cls,
        emitted_final_ranges: dict[str, list[tuple[float, float]]],
        emitted_final_records: list[tuple[str, float, float]],
        text: str,
        start_time: float,
        end_time: float,
    ) -> None:
        text_key = cls._final_key(text)
        if text_key:
            emitted_final_ranges.setdefault(text_key, []).append((start_time, end_time))
            emitted_final_records.append((text, start_time, end_time))

    async def connect(self):
        """Establece conexión WebSocket con Whisper Live server."""
        ws_url = f"ws://{self.host}:{self.port}"
        logger.info(f"🔗 Connecting to Whisper Live: {ws_url} (lang={self.language}, model={self.model})")

        self._closing = False
        self._server_error = None
        self._server_error_code = ""
        try:
            self._ws = await websockets.connect(
                ws_url,
                # Whisper Live's sync server can spend long stretches inside
                # blocking model/audio work and miss websocket-level ping/pong
                # deadlines even while the stream itself is healthy. Disable the
                # client heartbeat here and rely on send/recv failures instead.
                ping_interval=None,
                ping_timeout=None,
                max_size=None,
            )

            # Enviar configuración inicial (protocolo Whisper Live)
            config = {
                "uid": self.uid,
                "language": self.language,
                "model": self.model,
                "task": self.task,
                "use_vad": self.use_vad,
                "no_speech_thresh": self.no_speech_thresh,
                "clip_audio": self.clip_audio,
                "max_uncommitted_audio_s": self.max_uncommitted_audio_s,
                "context_overlap_s": self.context_overlap_s,
                "latency_logging": self.latency_logging,
                "caption_protocols": ["stable_text_prefix_v1"],
            }
            if self.same_output_threshold is not None:
                config["same_output_threshold"] = self.same_output_threshold
            if self.vad_parameters:
                config["vad_parameters"] = self.vad_parameters
            await self._ws.send(json.dumps(config))
            self._connected = True

            # Iniciar tarea de recepción en background
            self._receiver_task = asyncio.create_task(self._receive_loop())

            logger.info(f"✅ Whisper Live connected (uid={self.uid[:8]}...)")
            return self

        except Exception as e:
            logger.error(f"❌ Whisper Live connection error: {e}")
            raise

    async def _receive_loop(self):
        """Loop de recepción en background - pone mensajes en la queue."""
        if not self._ws:
            return

        try:
            async for message in self._ws:
                try:
                    data = json.loads(message)

                    # Manejar mensajes de control
                    msg_type = data.get("message", "")

                    if data.get("status") in {"ERROR", "WAIT"}:
                        code = data.get("error_code")
                        if data.get("status") == "WAIT" or code == "model_busy":
                            code = "model_busy"
                        elif code != "prefix_context_lost":
                            code = "asr_stream_failed" if self._server_ready else "model_load_failed"
                        self._server_error_code = code
                        self._server_error = ASR_ERROR_MESSAGES[code]
                        break
                    if data.get("status") == "LOADING":
                        logger.info("Whisper Live is preparing the selected model")
                        continue

                    if msg_type == "WAIT":
                        logger.info("⏳ Whisper Live: server is busy, waiting...")
                        continue

                    if msg_type == "SERVER_READY":
                        self._server_ready = True
                        self.model_service_id = data.get("model_service_id")
                        self._server_info = {
                            "backend": data.get("backend"),
                            "device": data.get("device"),
                            "compute_type": data.get("compute_type"),
                            "model": data.get("model"),
                        }
                        if data.get("caption_protocol"):
                            self._server_info["caption_protocol"] = data["caption_protocol"]
                        logger.info("✅ Whisper Live: server ready")
                        continue

                    if msg_type == "DISCONNECT":
                        logger.info("🔌 Whisper Live: server disconnected")
                        break

                    # Mensaje con segmentos de transcripción
                    await self._receive_queue.put(data)

                except json.JSONDecodeError as e:
                    logger.warning(f"Invalid JSON from Whisper Live: {e}")

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"Whisper Live connection closed: {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Whisper Live receive error: {e}")
        finally:
            if self._server_ready and not self._closing and not self._server_error:
                self._server_error_code = "asr_disconnected"
                self._server_error = ASR_ERROR_MESSAGES[self._server_error_code]
            self._connected = False
            # Señal de fin
            await self._receive_queue.put(None)

    async def send_audio(self, audio_data: bytes):
        """
        Envía chunk de audio PCM 16kHz al servidor.

        Whisper Live espera audio en formato float32 normalizado (-1.0 a 1.0).
        Recibimos int16 PCM desde LiveKit, así que convertimos antes de enviar.

        Args:
            audio_data: Bytes de audio PCM 16-bit signed little-endian, 16kHz, mono
        """
        if self._ws and self._connected:
            try:
                # Convertir int16 PCM a float32 normalizado (protocolo Whisper Live)
                raw_data = np.frombuffer(audio_data, dtype=np.int16)
                float32_data = (raw_data.astype(np.float32) / 32768.0)
                send_started_at = time.monotonic()
                await self._ws.send(float32_data.tobytes())
                if self._audio_started_at is None:
                    self._audio_started_at = send_started_at
                self._audio_samples_sent += raw_data.size
            except websockets.exceptions.ConnectionClosed:
                logger.warning("Connection closed while sending audio")
                self._connected = False
            except Exception as e:
                logger.debug(f"Audio send error: {e}")

    async def receive_transcriptions(self) -> AsyncIterator[TranscriptionChunk]:
        """
        Recibe transcripciones del servidor Whisper Live.

        Emite:
        - TranscriptionChunk con is_partial=True: texto parcial (UI en tiempo real)
        - TranscriptionChunk con is_partial=False: texto confirmado (traducción + TTS)

        Lógica:
        - Whisper Live envía segmentos [{text, start, end}]
        - ESTABLES (todos menos el último): ya no cambian → FINAL
        - ACTIVO (el último): Whisper lo reescribe → PARTIAL
        - Si el activo no cambia en ACTIVE_TIMEOUT_S, se finaliza (evita frase colgada)
        """
        active_timeout_s = self.active_timeout_s

        # Estado de tracking
        emitted_finals: set[tuple[str, float]] = set()  # Compatibility fast path for exact active matches.
        emitted_final_ranges: dict[str, list[tuple[float, float]]] = {}
        emitted_final_records: list[tuple[str, float, float]] = []
        pending_partial = ""         # Último parcial emitido (para deduplicar)
        pending_start_time = 0.0
        pending_end_time = 0.0
        last_text_change_at: Optional[float] = None
        loop = asyncio.get_running_loop()

        try:
            while True:
                # Finalizar cuando el texto activo lleva estable active_timeout_s.
                # Los mensajes repetidos de WhisperLive no reinician este plazo.
                receive_timeout: Optional[float] = None
                if pending_partial and last_text_change_at is not None:
                    elapsed_since_change = loop.time() - last_text_change_at
                    receive_timeout = max(0.0, active_timeout_s - elapsed_since_change)
                    if receive_timeout == 0.0:
                        if self._final_already_emitted(
                            emitted_final_ranges,
                            emitted_final_records,
                            pending_partial,
                            pending_start_time,
                            pending_end_time,
                        ):
                            pending_partial = ""
                            pending_start_time = 0.0
                            pending_end_time = 0.0
                            last_text_change_at = None
                            continue

                        last_char = pending_partial[-1] if pending_partial else ""
                        punct = last_char if last_char in CHUNK_PUNCTUATION else ""
                        is_sent_end = last_char in SENTENCE_END
                        latency_ms, chunk_id = self._final_metadata(pending_end_time)

                        yield TranscriptionChunk(
                            text=pending_partial,
                            language=self.language,
                            is_partial=False,
                            punctuation=punct,
                            is_sentence_end=is_sent_end,
                            start_time=pending_start_time,
                            end_time=pending_end_time,
                            latency_ms=latency_ms,
                            finalization_reason="timeout",
                            chunk_id=chunk_id,
                        )
                        emitted_finals.add((pending_partial, pending_end_time))
                        self._mark_final_emitted(
                            emitted_final_ranges,
                            emitted_final_records,
                            pending_partial,
                            pending_start_time,
                            pending_end_time,
                        )
                        logger.debug(f"📤 FINAL (timeout): '{pending_partial[:60]}'")
                        pending_partial = ""
                        pending_start_time = 0.0
                        pending_end_time = 0.0
                        last_text_change_at = None
                        continue

                # Esperar hasta el próximo mensaje o hasta el plazo de estabilización.
                try:
                    if receive_timeout is None:
                        data = await self._receive_queue.get()
                    else:
                        data = await asyncio.wait_for(
                            self._receive_queue.get(),
                            timeout=receive_timeout
                        )
                except asyncio.TimeoutError:
                    # Timeout: si hay un parcial pendiente, finalizarlo
                    if pending_partial and self._final_already_emitted(
                        emitted_final_ranges,
                        emitted_final_records,
                        pending_partial,
                        pending_start_time,
                        pending_end_time,
                    ):
                        pending_partial = ""
                        pending_start_time = 0.0
                        pending_end_time = 0.0
                        last_text_change_at = None
                    elif pending_partial:
                        last_char = pending_partial[-1] if pending_partial else ""
                        punct = last_char if last_char in CHUNK_PUNCTUATION else ""
                        is_sent_end = last_char in SENTENCE_END
                        latency_ms, chunk_id = self._final_metadata(pending_end_time)

                        yield TranscriptionChunk(
                            text=pending_partial,
                            language=self.language,
                            is_partial=False,
                            punctuation=punct,
                            is_sentence_end=is_sent_end,
                            start_time=pending_start_time,
                            end_time=pending_end_time,
                            latency_ms=latency_ms,
                            finalization_reason="timeout",
                            chunk_id=chunk_id,
                        )
                        emitted_finals.add((pending_partial, pending_end_time))
                        self._mark_final_emitted(
                            emitted_final_ranges,
                            emitted_final_records,
                            pending_partial,
                            pending_start_time,
                            pending_end_time,
                        )
                        logger.debug(f"📤 FINAL (timeout): '{pending_partial[:60]}'")
                        pending_partial = ""
                        pending_start_time = 0.0
                        pending_end_time = 0.0
                        last_text_change_at = None
                    continue

                if data is None:
                    if self._server_error:
                        raise ASRStreamError(self._server_error_code)
                    # Fin del stream — finalizar parcial pendiente
                    if pending_partial and not self._final_already_emitted(
                        emitted_final_ranges,
                        emitted_final_records,
                        pending_partial,
                        pending_start_time,
                        pending_end_time,
                    ):
                        last_char = pending_partial[-1] if pending_partial else ""
                        punct = last_char if last_char in CHUNK_PUNCTUATION else ""
                        latency_ms, chunk_id = self._final_metadata(pending_end_time)
                        yield TranscriptionChunk(
                            text=pending_partial,
                            language=self.language,
                            is_partial=False,
                            punctuation=punct,
                            is_sentence_end=True,
                            start_time=pending_start_time,
                            end_time=pending_end_time,
                            latency_ms=latency_ms,
                            finalization_reason="eos",
                            chunk_id=chunk_id,
                        )
                        emitted_finals.add((pending_partial, pending_end_time))
                        self._mark_final_emitted(
                            emitted_final_ranges,
                            emitted_final_records,
                            pending_partial,
                            pending_start_time,
                            pending_end_time,
                        )
                        logger.debug(f"📤 FINAL (eos): '{pending_partial[:60]}'")
                    break

                if "text_prefix" in data:
                    if self._server_info.get("caption_protocol") != "stable_text_prefix_v1":
                        raise ValueError("Unnegotiated ASR prefix protocol")
                    async for chunk in self._receive_text_prefix_transcriptions(data):
                        yield chunk
                    return

                # Extraer segmentos
                segments = data.get("segments", [])
                if not segments:
                    continue

                # Construir lista de segmentos no vacíos conservando metadatos del servidor.
                seg_items = []
                for seg in segments:
                    seg_text = seg.get("text", "").strip()
                    if seg_text:
                        start_time = self._segment_time(seg.get("start", 0.0))
                        end_time = self._segment_time(seg.get("end", 0.0))
                        if end_time <= start_time:
                            logger.debug(
                                "Skipping invalid Whisper segment start=%.3f end=%.3f text=%r",
                                start_time,
                                end_time,
                                seg_text[:80],
                            )
                            continue
                        seg_items.append({
                            "text": seg_text,
                            "start": start_time,
                            "end": end_time,
                            "completed": bool(seg.get("completed", False)),
                            "finalization_reason": seg.get("finalization_reason") or "stable",
                        })

                if not seg_items:
                    continue

                seg_texts = [seg["text"] for seg in seg_items]
                seg_times = [(seg["start"], seg["end"]) for seg in seg_items]

                # ─── SEGMENTOS ESTABLES/COMPLETADOS → FINAL ───
                n_stable = len(seg_items) if seg_items[-1]["completed"] else max(0, len(seg_items) - 1)

                for i in range(n_stable):
                    seg_text = seg_texts[i]
                    start_t, end_t = seg_times[i] if i < len(seg_times) else (0, 0)

                    # Deduplicar
                    if self._final_already_emitted(emitted_final_ranges, emitted_final_records, seg_text, start_t, end_t):
                        continue
                    last_char = seg_text[-1] if seg_text else ""
                    punct = last_char if last_char in CHUNK_PUNCTUATION else ""
                    is_sent_end = last_char in SENTENCE_END
                    latency_ms, chunk_id = self._final_metadata(end_t)

                    yield TranscriptionChunk(
                        text=seg_text,
                        language=self.language,
                        is_partial=False,
                        punctuation=punct,
                        is_sentence_end=is_sent_end,
                        start_time=start_t,
                        end_time=end_t,
                        latency_ms=latency_ms,
                        finalization_reason=seg_items[i]["finalization_reason"],
                        chunk_id=chunk_id,
                    )
                    emitted_finals.add((seg_text, end_t))
                    self._mark_final_emitted(emitted_final_ranges, emitted_final_records, seg_text, start_t, end_t)
                    pending_partial = ""  # Ya no hay parcial pendiente
                    pending_start_time = 0.0
                    pending_end_time = 0.0
                    last_text_change_at = None
                    logger.debug(f"📤 FINAL (stable): '{seg_text[:60]}'")

                if n_stable >= len(seg_texts):
                    continue

                # ─── ÚLTIMO SEGMENTO (activo) → PARTIAL ───
                active_text = seg_texts[-1]
                active_start, active_end = seg_times[-1] if seg_times else (0, 0)

                # Si ya emitido como FINAL, skip
                if self._final_already_emitted(emitted_final_ranges, emitted_final_records, active_text, active_start, active_end):
                    continue

                pending_start_time = active_start
                pending_end_time = active_end

                # Emitir como PARTIAL solo si cambió
                if active_text != pending_partial:
                    pending_partial = active_text
                    last_text_change_at = loop.time()

                    yield TranscriptionChunk(
                        text=active_text,
                        language=self.language,
                        is_partial=True,
                        start_time=active_start,
                        end_time=active_end,
                    )
                    logger.debug(f"📤 PARTIAL: '{active_text[:60]}'")

        except asyncio.CancelledError:
            pass
        except ASRStreamError:
            raise
        except Exception as e:
            logger.error("Transcription processing error: %s", type(e).__name__)
            raise ASRStreamError("asr_protocol_error") from None

    async def _receive_text_prefix_transcriptions(self, first) -> AsyncIterator[TranscriptionChunk]:
        """Consume immutable, ordered prefixes without pretending they are timed.

        Repeated text with a new ID is real new output. Replayed IDs are ignored;
        gaps and revisions to closed IDs fail explicitly. There is no timeout
        confirmation of a mutable draft in this protocol.
        """
        update_sequence = 0
        confirmed_sequence = 0
        seen = {}
        pending = ""
        data = first

        def chunk(text, partial=False, reason="stable_prefix", sequence=0):
            last = text[-1:]
            return TranscriptionChunk(text=text, language=self.language, is_partial=partial,
                punctuation=last if last in CHUNK_PUNCTUATION else "",
                is_sentence_end=last in SENTENCE_END, finalization_reason=reason,
                chunk_id=f"{self.uid}:prefix:{sequence}", prefix_protocol=True)

        while data is not None:
            payload = data.get("text_prefix")
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("Invalid ASR prefix protocol version")
            seq = payload.get("sequence")
            if type(seq) is not int or seq <= 0:
                raise ValueError("Invalid ASR prefix update sequence")
            if seq > update_sequence:
                entries = payload.get("confirmed")
                after = payload.get("after_confirmed")
                draft = payload.get("draft")
                if not isinstance(entries, list) or type(after) is not int or not isinstance(draft, str):
                    raise ValueError("Invalid ASR prefix payload")
                new = []
                expected = confirmed_sequence
                previous_id = 0
                for item in entries:
                    number, text = item.get("sequence"), item.get("text")
                    reason = item.get("finalization_reason", "stable_prefix")
                    if (type(number) is not int or number <= previous_id
                            or not isinstance(text, str) or not text.strip()
                            or not isinstance(reason, str)
                            or reason not in {"stable_prefix", "vad_pause_recovery"}):
                        raise ValueError("Invalid confirmed ASR prefix")
                    previous_id = number
                    if number <= confirmed_sequence:
                        if number in seen and seen[number] != (text, reason):
                            raise ValueError("Closed ASR prefix was revised")
                        continue
                    if number != expected + 1:
                        raise ValueError("ASR prefix gap; refusing to drop text")
                    new.append((number, text, reason))
                    expected = number
                if after != expected or after < confirmed_sequence:
                    raise ValueError("ASR draft has inconsistent confirmed watermark")
                # Validate the whole packet before publishing any part of it.
                for number, text, reason in new:
                    seen[number] = (text, reason)
                    yield chunk(text, reason=reason, sequence=number)
                seen = {n: text for n, text in seen.items() if n > expected - 10}
                confirmed_sequence = expected
                if draft != pending or new:
                    pending = draft
                    if draft:
                        yield chunk(draft, partial=True, sequence=expected + 1)
                update_sequence = seq
            data = await self._receive_queue.get()
        if self._server_error:
            raise ASRStreamError(self._server_error_code)
        if pending:
            yield chunk(pending, reason="eos", sequence=confirmed_sequence + 1)

    async def wait_until_ready(self, timeout: float = 15.0) -> dict[str, Any]:
        """Wait until the server signals that ASR is ready and return its metadata."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self._connected and not self._server_ready:
            if self._server_error:
                raise ConnectionError(self._server_error)
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("Timed out waiting for Whisper Live server readiness")
            await asyncio.sleep(0.05)
        if self._server_error:
            raise ConnectionError(self._server_error)
        if not self._connected:
            raise ConnectionError("Whisper Live connection closed before server became ready")
        return self._server_info

    async def close(self):
        """Cierra la conexión enviando señal de fin al servidor."""
        self._closing = True
        self._connected = False
        self._server_ready = False
        self._server_info = {}

        # Enviar señal END_OF_AUDIO al servidor (protocolo Whisper Live)
        if self._ws:
            try:
                await self._ws.send(b"END_OF_AUDIO")
                # Dar tiempo al servidor para procesar el último audio
                await asyncio.sleep(0.5)
            except:
                pass

        if self._receiver_task and not self._receiver_task.done():
            self._receiver_task.cancel()
            try:
                await asyncio.wait_for(self._receiver_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._receiver_task = None

        if self._ws:
            try:
                await self._ws.close()
            except:
                pass
            self._ws = None

        self._audio_started_at = None
        self._audio_samples_sent = 0
        self._final_chunk_sequence = 0

        logger.info("🔌 Whisper Live disconnected")

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_ready(self) -> bool:
        return self._connected and self._server_ready

    @property
    def server_info(self) -> dict[str, Any]:
        return dict(self._server_info)
