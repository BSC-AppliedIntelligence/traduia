import os
import json
import logging
import queue
from types import SimpleNamespace
import threading
import time
import torch
import ctranslate2
from huggingface_hub import snapshot_download

from whisper_live.transcriber.transcriber_faster_whisper import WhisperModel
from whisper_live.backend.base import ServeClientBase
from whisper_live.stable_prefix import BSC_MODEL, StableWordPrefix, RetainedWordPrefix, PrefixContextLost
from whisper_live.stable_prefix import aligned_words
from whisper_live.fast_prefix import FastTextPrefix, PROTOCOL, accepted_text, align_text


def experimental_fast_prefix(model):
    """Opt in selected Systran models without changing their decoding policy."""
    allowed = {
        "small": {"small", "Systran/faster-whisper-small"},
        "systran-large": {"Systran/faster-whisper-large-v3", "large-v3"},
    }
    selected = {key.strip() for key in os.environ.get(
        "WHISPER_EXPERIMENTAL_PREFIX_MODELS", "").split(",") if key.strip()}
    if selected - allowed.keys():
        from whisper_live.model_selection import ModelSelectionError
        raise ModelSelectionError("unsupported_prefix_model",
                                  "Experimental prefix models must be small or systran-large.")
    return any(model in allowed[key] for key in selected)


def configured_fast_prefix(model):
    """Resolve the release policy by model; keep legacy research flags readable.

    The deployment profile supplies the list, so standalone/older clients keep
    their existing defaults. An explicit 'none' selects the timestamp control.
    """
    setting = os.environ.get("WHISPER_PREFIX_MODELS", "").strip()
    if setting:
        allowed = {
            "small": {"small", "Systran/faster-whisper-small"},
            "systran-large": {"large-v3", "Systran/faster-whisper-large-v3"},
            "bsc-large": {BSC_MODEL},
        }
        if setting == "none":
            return False
        selected = {key.strip() for key in setting.split(",") if key.strip()}
        if not selected or selected - allowed.keys():
            from whisper_live.model_selection import ModelSelectionError
            raise ModelSelectionError("unsupported_prefix_model",
                                      "Prefix models must be small, systran-large, bsc-large or none.")
        return any(model in allowed[key] for key in selected)
    return ((os.environ.get("WHISPER_BSC_STABLE_PREFIX") == "fast_text_prefix"
             and model == BSC_MODEL) or experimental_fast_prefix(model))


class ServeClientFasterWhisper(ServeClientBase):
    SINGLE_MODEL = None
    SINGLE_MODEL_LOCK = threading.Lock()
    BATCH_WORKER = None

    def __init__(
        self,
        websocket,
        task="transcribe",
        device=None,
        language=None,
        client_uid=None,
        model="small.en",
        initial_prompt=None,
        vad_parameters=None,
        use_vad=True,
        single_model=False,
        send_last_n_segments=10,
        no_speech_thresh=0.45,
        clip_audio=False,
        same_output_threshold=7,
        cache_path="~/.cache/whisper-live/",
        translation_queue=None,
        max_uncommitted_audio_s=0.0,
        context_overlap_s=1.0,
        latency_logging=False,
        model_selection=None,
        caption_protocols=None,
    ):
        """
        Initialize a ServeClient instance.
        The Whisper model is initialized based on the client's language and device availability.
        The transcription thread is started upon initialization. A "SERVER_READY" message is sent
        to the client to indicate that the server is ready.

        Args:
            websocket (WebSocket): The WebSocket connection for the client.
            task (str, optional): The task type, e.g., "transcribe". Defaults to "transcribe".
            device (str, optional): The device type for Whisper, "cuda" or "cpu". Defaults to None.
            language (str, optional): The language for transcription. Defaults to None.
            client_uid (str, optional): A unique identifier for the client. Defaults to None.
            model (str, optional): The whisper model size. Defaults to 'small.en'
            initial_prompt (str, optional): Prompt for whisper inference. Defaults to None.
            single_model (bool, optional): Whether to instantiate a new model for each client connection. Defaults to False.
            send_last_n_segments (int, optional): Number of most recent segments to send to the client. Defaults to 10.
            no_speech_thresh (float, optional): Segments with no speech probability above this threshold will be discarded. Defaults to 0.45.
            clip_audio (bool, optional): Whether to clip audio with no valid segments. Defaults to False.
            same_output_threshold (int, optional): Number of repeated outputs before considering it as a valid segment. Defaults to 10.

        """
        super().__init__(
            client_uid,
            websocket,
            send_last_n_segments,
            no_speech_thresh,
            clip_audio,
            same_output_threshold,
            translation_queue,
            max_uncommitted_audio_s,
            context_overlap_s,
            latency_logging,
        )
        self.cache_path = cache_path
        self.model_sizes = [
            "tiny", "tiny.en", "base", "base.en", "small", "small.en",
            "medium", "medium.en", "large-v2", "large-v3", "distil-small.en",
            "distil-medium.en", "distil-large-v2", "distil-large-v3",
            "large-v3-turbo", "turbo"
        ]

        self.model_size_or_path = model_selection.status()["selected_model"] if model_selection is not None else model
        self.model_selection = model_selection
        self._model_lease = None
        self.language = ("en" if model_selection is None and self.model_size_or_path.endswith("en") else language)
        self.task = task
        self.initial_prompt = initial_prompt
        self.vad_parameters = vad_parameters or {"threshold": 0.5}
        fast_mode = configured_fast_prefix(self.model_size_or_path)
        if fast_mode and (PROTOCOL not in (caption_protocols or [])
                          or ServeClientFasterWhisper.BATCH_WORKER is not None
                          or task != "transcribe" or language is None
                          or max_uncommitted_audio_s <= 0 or clip_audio
                          or translation_queue is not None):
            from whisper_live.model_selection import ModelSelectionError
            raise ModelSelectionError("unsupported_prefix_client",
                                      "Stable-prefix confirmation requires a compatible unbatched caption client and a bounded audio window.")

        device = "cpu"
        if torch.cuda.is_available():
            try:
                ctranslate2.get_supported_compute_types("cuda")
                device = "cuda"
            except Exception as e:
                logging.warning(
                    "Torch reports CUDA is available but CTranslate2 cannot use it (%s). Falling back to CPU.",
                    e,
                )
        if device == "cuda":
            major, _ = torch.cuda.get_device_capability(device)
            self.compute_type = "float16" if major >= 7 else "float32"
        else:
            self.compute_type = "int8"

        self.device = device

        if self.model_size_or_path is None:
            return
        logging.info(f"Using Device={device} with precision {self.compute_type}")
    
        try:
            if model_selection is not None:
                def load_selected(model_id):
                    self.model_size_or_path = model_id
                    self.create_model(device)
                    return self.transcriber

                def unload_previous(transcriber):
                    # All client inference threads have drained before their
                    # leases are released. Do not keep an old GPU model alive.
                    transcriber.model.unload_model()

                self.websocket.send(json.dumps({"uid": self.client_uid, "status": "LOADING",
                                                "message": "Preparing the selected ASR model."}))
                self._model_lease = model_selection.acquire(load_selected, unload_previous)
                self.transcriber = self._model_lease.model
                self.model_size_or_path = self._model_lease.model_id
            elif single_model:
                if ServeClientFasterWhisper.SINGLE_MODEL is None:
                    self.create_model(device)
                    ServeClientFasterWhisper.SINGLE_MODEL = self.transcriber
                else:
                    self.transcriber = ServeClientFasterWhisper.SINGLE_MODEL
            else:
                self.create_model(device)
        except Exception as e:
            if model_selection is not None:
                # The server returns typed/sanitized errors; do not add a
                # half-initialized client or leak a reservation on failure.
                self.transcriber = None
                raise
            logging.error(f"Failed to load model: {e}")
            self.websocket.send(json.dumps({
                "uid": self.client_uid,
                "status": "ERROR",
                "message": f"Failed to load model: {str(self.model_size_or_path)}"
            }))
            self.websocket.close()
            return

        self.use_vad = use_vad

        # The release profile enables supported models automatically. Systran
        # keeps native timestamp decoding; only BSC's prefix path disables it.
        self.stable_prefix = None
        prefix_mode = (os.environ.get("WHISPER_BSC_STABLE_PREFIX")
                       if not os.environ.get("WHISPER_PREFIX_MODELS", "").strip() else None)
        if fast_mode:
            self.stable_prefix = FastTextPrefix(max_recovery_span_s=max(.5, self.context_overlap_s))
            self._fast_last_commit = None
            logging.info("ASR_POLICY uid=%s policy=fast_text_prefix decoding=%s",
                         self.client_uid, "text_only" if self.model_size_or_path == BSC_MODEL
                         else "native_timestamps")
        if prefix_mode in {"1", "retain_context", "retain_context_joint_gate"} and self.model_size_or_path == BSC_MODEL:
            if ServeClientFasterWhisper.BATCH_WORKER is not None:
                logging.warning("Stable-prefix research disabled for batched inference")
            else:
                self.stable_prefix = (RetainedWordPrefix(joint_speech_gate=prefix_mode == "retain_context_joint_gate")
                                      if prefix_mode != "1" else StableWordPrefix())
                logging.info("ASR_POLICY uid=%s policy=%s alignment=enabled", self.client_uid, prefix_mode)

        # threading
        self.trans_thread = threading.Thread(target=self.speech_to_text)
        try:
            self.trans_thread.start()
            self.websocket.send(
                json.dumps({
                    "uid": self.client_uid, "message": self.SERVER_READY,
                    "backend": "faster_whisper", "device": self.device,
                    "compute_type": self.compute_type, "model": self.model_size_or_path,
                    "model_service_id": self.model_selection.service_id if self.model_selection else None,
                    **({"caption_protocol": PROTOCOL} if fast_mode else {}),
                })
            )
        except Exception:
            self.cleanup()
            raise

    def cleanup(self):
        super().cleanup()
        lease, self._model_lease = self._model_lease, None
        if lease is not None:
            def drain():
                # Returning from a websocket is not proof that GPU inference
                # ended. Keep the admission lease until its thread really exits.
                if self.trans_thread.ident is not None:
                    self.trans_thread.join()
                self.transcriber = None
                lease.close()
            threading.Thread(target=drain, daemon=True).start()

    def create_model(self, device):
        """
        Instantiates a new model, sets it as the transcriber. If model is a huggingface model_id
        then it is automatically converted to ctranslate2(faster_whisper) format.
        """
        model_ref = self.model_size_or_path

        if model_ref in self.model_sizes:
            model_to_load = model_ref
        else:
            logging.info(f"Model not in model_sizes")
            if os.path.isdir(model_ref) and ctranslate2.contains_model(model_ref):
                model_to_load = model_ref
            else:
                local_snapshot = snapshot_download(
                    repo_id = model_ref,
                    repo_type = "model",
                )
                if ctranslate2.contains_model(local_snapshot):
                    model_to_load = local_snapshot
                else:
                    cache_root = os.path.expanduser(os.path.join(self.cache_path, "whisper-ct2-models/"))
                    os.makedirs(cache_root, exist_ok=True)
                    safe_name = model_ref.replace("/", "--")
                    ct2_dir = os.path.join(cache_root, safe_name)

                    if not ctranslate2.contains_model(ct2_dir):
                        logging.info(f"Converting '{model_ref}' to CTranslate2 @ {ct2_dir}")
                        ct2_converter = ctranslate2.converters.TransformersConverter(
                            local_snapshot, 
                            copy_files=["tokenizer.json", "preprocessor_config.json"]
                        )
                        ct2_converter.convert(
                            output_dir=ct2_dir,
                            quantization=self.compute_type,
                            force=False,  # skip if already up-to-date
                        )
                    model_to_load = ct2_dir

        logging.info(f"Loading model: {model_to_load}")
        self.transcriber = WhisperModel(
            model_to_load,
            device=device,
            compute_type=self.compute_type,
            local_files_only=False,
        )

    def set_language(self, info):
        """
        Updates the language attribute based on the detected language information.

        Args:
            info (object): An object containing the detected language and its probability. This object
                        must have at least two attributes: `language`, a string indicating the detected
                        language, and `language_probability`, a float representing the confidence level
                        of the language detection.
        """
        if info.language_probability > 0.5:
            self.language = info.language
            logging.info(f"Detected language {self.language} with probability {info.language_probability}")
            self.websocket.send(json.dumps(
                {"uid": self.client_uid, "language": self.language, "language_prob": info.language_probability}))

    def transcribe_audio(self, input_sample):
        """
        Transcribes the provided audio sample using the configured transcriber instance.

        If the language has not been set, it updates the session's language based on the transcription
        information.

        Args:
            input_sample (np.array): The audio chunk to be transcribed. This should be a NumPy
                                    array representing the audio data.

        Returns:
            The transcription result from the transcriber. The exact format of this result
            depends on the implementation of the `transcriber.transcribe` method but typically
            includes the transcribed text.
        """
        # Batch inference path: submit to central queue and wait
        if ServeClientFasterWhisper.BATCH_WORKER is not None:
            if isinstance(self.stable_prefix, FastTextPrefix):
                self.exit = True
                self.websocket.send(json.dumps({'uid': self.client_uid, 'status': 'ERROR',
                    'error_code': 'unsupported_prefix_client',
                    'message': 'Fast-prefix research does not support batch inference.'}))
                self.websocket.close()
                raise PrefixContextLost('batch worker attached to prefix-protocol stream')
            if self.stable_prefix is not None:
                # The worker can be attached after the first client is created.
                logging.warning("Stable-prefix research disabled for batched inference")
                self.stable_prefix = None
            from whisper_live.batch_inference import BatchRequest
            request = BatchRequest(
                audio=input_sample,
                language=self.language,
                task=self.task,
                initial_prompt=self.initial_prompt,
                use_vad=self.use_vad,
                vad_parameters=self.vad_parameters if self.use_vad else None,
            )
            ServeClientFasterWhisper.BATCH_WORKER.submit(request)
            request.future.wait(timeout=30)
            if request.error:
                raise request.error
            if self.language is None and request.info is not None:
                self.set_language(request.info)
            return request.result

        # Preserve shared-model inference serialization in managed mode too.
        lock = (self.model_selection.inference_lock if self.model_selection is not None else
                ServeClientFasterWhisper.SINGLE_MODEL_LOCK if ServeClientFasterWhisper.SINGLE_MODEL else None)
        if lock is not None:
            lock.acquire()
        try:
            fast = isinstance(self.stable_prefix, FastTextPrefix)
            if fast:
                self._fast_samples = input_sample.copy()
            alignment_options = {"word_timestamps": True} if self.stable_prefix is not None and not fast else {}
            if fast and self.model_size_or_path == BSC_MODEL:
                # This checkpoint's native timestamp generation can trigger
                # long fallback decoding even on short inputs. Prefix IDs do
                # not use those times; align existing text only when needed.
                alignment_options['without_timestamps'] = True
            result, info = self.transcriber.transcribe(
                input_sample,
                initial_prompt=self.initial_prompt,
                language=self.language,
                task=self.task,
                vad_filter=self.use_vad,
                vad_parameters=self.vad_parameters if self.use_vad else None,
                **alignment_options)
        finally:
            if lock is not None:
                lock.release()

        if self.language is None and info is not None:
            self.set_language(info)
        # The base loop advances past VAD-empty audio. Never let that discard
        # an outstanding fast-prefix draft/context without an explicit decision.
        if fast and result is None and (self.stable_prefix.confirmed or self.stable_prefix.draft):
            return []
        return result

    def get_audio_chunk_for_processing(self):
        samples, duration = super().get_audio_chunk_for_processing()
        if isinstance(getattr(self, 'stable_prefix', None), FastTextPrefix):
            # Slow inference can receive another full utterance meanwhile.
            # Bound this inference snapshot, not the stored audio: later speech
            # remains queued, and alignment never receives an oversized window.
            limit = min(25., self.max_uncommitted_audio_s * 2)
            if duration > limit:
                logging.info('ASR_FAST_INPUT_CAP uid=%s queued_s=%.3f input_s=%.3f',
                             self.client_uid, duration, limit)
                samples = samples[:int(limit * self.RATE)]
                duration = len(samples) / self.RATE
        return samples, duration

    def _buffer_trim_samples(self):
        limit = super()._buffer_trim_samples()
        if isinstance(getattr(self, 'stable_prefix', None), FastTextPrefix):
            # timestamp_offset is the prefix policy's retired-audio boundary,
            # not the end of its published text. Recovery still needs everything
            # after it. The prefix loop's hard limit stops unsafe growth rather
            # than advancing that boundary and silently discarding speech.
            retired = int(max(0., self.timestamp_offset - self.frames_offset) * self.RATE)
            return min(limit, retired)
        return limit

    def _fast_align(self, samples, offset, text):
        """Measured separately by the harness; never use native BSC times."""
        lock = (self.model_selection.inference_lock if self.model_selection is not None
                else ServeClientFasterWhisper.SINGLE_MODEL_LOCK)
        with lock:
            return align_text(self.transcriber, samples, text, self.language, offset)

    def _fast_reconcile(self, aligned, audio_end=None):
        policy = self.stable_prefix
        count = policy.align_context(aligned)
        if count is None and self._fast_last_commit is not None:
            samples, offset, text = self._fast_last_commit
            old = self._fast_align(samples, offset, text)
            n = policy.prefix_length([w.text for w in old])
            if n is not None:
                policy.timed = old[:n]
                count = policy.align_context(aligned)
                if count is None and audio_end is not None:
                    count = policy.recover_short_context(old, aligned, audio_end)
        if count is None and audio_end is not None:
            count = policy.recover_closed_audio(aligned, audio_end, offset=self.timestamp_offset,
                                                overlap_s=max(self.context_overlap_s, .5))
        if count is None and audio_end is not None:
            count = policy.reopen_rewritten_context(aligned, audio_end)
            if count is not None:
                # The old snapshot contains disputed text, not an acoustic
                # authority for the reopened suffix. Save a new one only when
                # ordinary agreement next commits text. No audio is retired.
                self._fast_last_commit = None
        if count is None:
            if self.latency_logging:
                logging.info('ASR_FAST_CONTEXT_REJECT uid=%s detail=%s', self.client_uid, json.dumps({
                    'offset': self.timestamp_offset,
                    'confirmed': policy.confirmed,
                    'old_words': [w.__dict__ for w in policy.timed],
                    'new_words': [w.__dict__ for w in aligned],
                    'commit_text': self._fast_last_commit[2] if self._fast_last_commit else None,
                }, ensure_ascii=False))
            raise PrefixContextLost('confirmed text could not be located safely in retained audio')
        if audio_end is not None:
            identity = (self.timestamp_offset, audio_end, len(policy.confirmed))
            cached = getattr(self, '_fast_checked_trim', (None, None, None, None))
            if cached[:3] != identity:
                cut = policy.checked_short_trim(aligned, self.timestamp_offset, audio_end,
                                                max(self.context_overlap_s, .5))
                # A second reconciliation after new text commits may expand
                # the candidate set on the SAME snapshot. Record that evidence
                # but retain any cut already checked against an earlier one.
                if cut is None and cached[:2] == identity[:2]:
                    cut = cached[-1]
                self._fast_checked_trim = (*identity, cut)
        return [w.text for w in aligned]

    def _recover_at_pause(self, duration):
        """Close the oldest completed utterance while preserving later audio.

        Only used when ordinary prefix progress stalls. A real VAD silence
        between voiced regions provides the endpoint; re-decode just that
        completed audio, never finalize a guess from an unfinished window.
        """
        if not self.use_vad or duration < self.max_uncommitted_audio_s:
            return False
        from faster_whisper.vad import get_speech_timestamps, VadOptions
        samples = self._fast_samples
        speech = get_speech_timestamps(samples, VadOptions(
            threshold=self.vad_parameters.get('threshold', .5), speech_pad_ms=0,
            min_silence_duration_ms=500))
        policy = self.stable_prefix
        offset = self.timestamp_offset
        closed_end = policy.timed[-1].end if policy.timed else offset
        gaps = [(a['end'], b['start']) for a, b in zip(speech, speech[1:])
                if (b['start'] - a['end']) / self.RATE >= .6
                and offset + a['end'] / self.RATE >= closed_end - .02
                and a['end'] / self.RATE >= policy.min_new_audio_s]
        if not gaps:
            return False
        start, end = gaps[0]
        take = (start + end) // 2
        cut = offset + take / self.RATE
        attempted = getattr(self, '_fast_pause_attempted', None)
        identity = (offset, cut, tuple(policy.confirmed))
        if (attempted is not None and attempted[0] == offset
                and abs(attempted[1] - cut) <= .1 and attempted[2] == identity[2]):
            return False
        self._fast_pause_attempted = identity
        try:
            # This uses the same model/decoder options and inference lock.
            # transcribe_audio updates its snapshot; restore the live snapshot
            # even on rejection, without touching concurrently received frames.
            final = self.transcribe_audio(samples[:take])
        except Exception as exc:
            logging.warning('ASR_FAST_PAUSE_REJECT uid=%s error_type=%s',
                            self.client_uid, type(exc).__name__)
            return False
        finally:
            self._fast_samples = samples
        final = list(final) if final is not None else []
        if not final or any(getattr(s, 'no_speech_prob', 0) > self.no_speech_thresh
                            and getattr(s, 'avg_logprob', -float('inf')) <= -1 for s in final):
            return False
        items = accepted_text(final, self.no_speech_thresh)
        if not items:
            return False
        try:
            aligned = self._fast_align(samples[:take], offset, ''.join(items))
        except PrefixContextLost:
            return False
        count = policy.align_context(aligned)
        if count is None:
            # A short closed tail may itself have been corrected. Suppressing
            # unmatched words could eat new speech. Re-emit that bounded
            # utterance correction instead, explicitly marked as pause recovery;
            # existing published IDs/text remain untouched. Longer/unknown
            # context still fails closed rather than duplicating it arbitrarily.
            if (not 0 < len(policy.confirmed) <= 8
                    or len(policy.timed) != len(policy.confirmed)
                    or not 0 < policy.timed[-1].end - policy.timed[0].start
                              <= policy.max_recovery_span_s):
                return False
            logging.info('ASR_FAST_PAUSE_REPLAY uid=%s old_context_words=%d',
                         self.client_uid, len(policy.confirmed))
            policy.confirmed, policy.timed = [], []
        items = [w.text for w in aligned]
        new = policy.close_utterance(items)
        with self.lock:
            self.timestamp_offset = cut
        policy.move_window(cut)
        self._fast_last_commit = None
        self.websocket.send(json.dumps({'uid': self.client_uid, 'text_prefix': policy.packet()}))
        logging.info('ASR_FAST_PAUSE_RECOVERY uid=%s before=%.3f after=%.3f words=%d',
                     self.client_uid, offset, cut, len(new))
        return True

    def _handle_fast_prefix(self, result, duration):
        policy = self.stable_prefix
        offset = self.timestamp_offset
        try:
            items = accepted_text(result, self.no_speech_thresh)
            aligned = None
            if not items:
                # VAD-empty with retained speech is not permission to skip it.
                if duration >= self.max_uncommitted_audio_s:
                    raise PrefixContextLost('VAD rejected retained context at window limit')
                return
            if policy.prefix_length(items) is None:
                aligned = self._fast_align(self._fast_samples, offset, ''.join(items))
                items = self._fast_reconcile(aligned, offset + duration)
                # Recovered closed context is not new speech. Keep the pending
                # suffix evidence; exact lexical agreement below still guards
                # any new commit across different, growing audio snapshots.
            quiet = False
            if self.use_vad and policy.previous and len(items) - len(policy.confirmed) <= policy.hold_words:
                from faster_whisper.vad import get_speech_timestamps, VadOptions
                speech = get_speech_timestamps(self._fast_samples,
                    VadOptions(threshold=self.vad_parameters.get('threshold', .5), speech_pad_ms=0,
                               min_silence_duration_ms=500))
                quiet = bool(speech and duration - speech[-1]['end'] / self.RATE >= .6)
            committed = policy.observe(items, offset + duration, quiet)
            if committed:
                self._fast_last_commit = (self._fast_samples.copy(), offset, ''.join(items))
                logging.info('ASR_FAST_PREFIX uid=%s sequence=%d words=%d audio_end=%.3f text=%r',
                    self.client_uid, policy.sequence, len(committed), offset + duration, ''.join(committed))
            self.websocket.send(json.dumps({'uid': self.client_uid, 'text_prefix': policy.packet()}))
            if (quiet and committed and not policy.draft and not policy.previous
                    and all(not (getattr(s, 'no_speech_prob', 0) > self.no_speech_thresh
                                 and getattr(s, 'avg_logprob', -float('inf')) <= -1)
                            for s in result)):
                # Agreement closed every word and raw VAD verified trailing
                # silence. Keeping this processed utterance until the soft
                # limit lets a later decode rewrite it and strand recovery.
                # Retire only this snapshot, never audio received meanwhile;
                # no word-alignment timestamp is needed for this boundary.
                cut = offset + duration
                with self.lock:
                    self.timestamp_offset = cut
                policy.move_window(cut)
                self._fast_last_commit = None
                logging.info('ASR_FAST_SILENCE_TRIM uid=%s through_s=%.3f reason=closed_utterance',
                             self.client_uid, cut)
                return
            if duration >= self.max_uncommitted_audio_s:
                hard_limit = min(25., self.max_uncommitted_audio_s * 2)
                if not policy.confirmed:
                    if self._recover_at_pause(duration):
                        return
                    if duration >= hard_limit:
                        raise PrefixContextLost('no confirmed text before hard audio limit')
                    logging.info('ASR_FAST_DEFER uid=%s reason=awaiting_agreement input_s=%.3f hard_limit_s=%.3f',
                                 self.client_uid, duration, hard_limit)
                    return
                if aligned is None:
                    aligned = self._fast_align(self._fast_samples, offset, ''.join(items))
                self._fast_reconcile(aligned, offset + duration)
                target = offset + duration - max(self.context_overlap_s, .5)
                cuts = [w.end for i, w in enumerate(policy.timed)
                        if offset + policy.min_new_audio_s <= w.end <= target and w.end > w.start
                        and (i + 1 >= len(aligned) or w.end <= aligned[i + 1].start)
                        # Time overlap alone can leave a lone conjunction or
                        # fragment as all remaining confirmed context. A later
                        # decode may omit it, with no lexical anchor to recover.
                        # Keep a short phrase as well as the audio overlap.
                        and (len(policy.timed)-i-1 >= 4
                             or (i+1 == len(policy.timed) and not policy.draft))]
                if not cuts:
                    checked = self._fast_checked_trim[-1]
                    if checked is not None:
                        cuts = [checked]
                        logging.info('ASR_FAST_CHECKED_TRIM uid=%s through_s=%.3f',
                                     self.client_uid, checked)
                if not cuts:
                    if self._recover_at_pause(duration):
                        return
                    if self.latency_logging:
                        logging.info('ASR_FAST_BOUNDARY_WAIT uid=%s detail=%s', self.client_uid,
                                     json.dumps({'offset': offset, 'input_s': duration,
                                         'confirmed': policy.confirmed,
                                         'timed': [w.__dict__ for w in policy.timed],
                                         'draft': policy.draft}, ensure_ascii=False))
                    if duration >= hard_limit:
                        raise PrefixContextLost('no safe confirmed boundary before hard audio limit')
                    logging.info('ASR_FAST_DEFER uid=%s reason=awaiting_safe_boundary input_s=%.3f hard_limit_s=%.3f',
                                 self.client_uid, duration, hard_limit)
                    return
                cut = max(cuts)
                if (not policy.draft and cut >= policy.timed[-1].end - .001
                        and self.use_vad):
                    # When every word is closed and only silence follows, a
                    # word-end cut can leave a tiny trailing phoneme behind.
                    # Decoding that remnant alone can hallucinate subtitles.
                    # Retire this fully processed snapshot through its verified
                    # silence, never audio that arrived during inference.
                    from faster_whisper.vad import get_speech_timestamps, VadOptions
                    speech = get_speech_timestamps(self._fast_samples,
                        VadOptions(threshold=self.vad_parameters.get('threshold', .5),
                                   speech_pad_ms=0, min_silence_duration_ms=500))
                    if not speech or duration - speech[-1]['end'] / self.RATE >= .6:
                        cut = offset + duration
                        logging.info('ASR_FAST_SILENCE_TRIM uid=%s through_s=%.3f', self.client_uid, cut)
                with self.lock:
                    self.timestamp_offset = cut
                policy.move_window(cut)
                self._fast_last_commit = None
                logging.info('ASR_FAST_TRIM uid=%s before=%.3f after=%.3f retained_s=%.3f',
                             self.client_uid, offset, cut, offset + duration - cut)
        except PrefixContextLost as exc:
            if self._recover_at_pause(duration):
                return
            # A single growing hypothesis can temporarily rewrite its closed
            # context. Keep all audio and wait for fresh evidence; never turn
            # one mismatch into dropped speech or an immediate disconnected mic.
            if duration < min(25., self.max_uncommitted_audio_s * 2):
                logging.info('ASR_FAST_CONTEXT_WAIT uid=%s input_s=%.3f reason=%s',
                             self.client_uid, duration, exc)
                return
            self.exit = True
            self.websocket.send(json.dumps({'uid': self.client_uid, 'status': 'ERROR',
                'error_code': 'prefix_context_lost', 'message': 'Fast-prefix research stopped: unsafe retained context.'}))
            self.websocket.close()
            raise

    def update_segments(self, segments, duration):
        policy = getattr(self, "stable_prefix", None)
        if policy is None:
            return super().update_segments(segments, duration)
        if isinstance(policy, RetainedWordPrefix):
            return self._update_retained_prefix(segments, duration, policy)
        if self.bounded_window_enabled() and duration >= self.max_uncommitted_audio_s:
            # A tiny agreeing prefix must not defeat the existing hard reset
            # when inference is slower than audio arrival.
            policy.reset()
            return super().update_segments(segments, duration)
        offset = self.timestamp_offset
        decision = policy.observe(segments, offset, duration, self.no_speech_thresh)
        if decision is None:
            logging.info("ASR_PREFIX_FALLBACK uid=%s reason=unsafe_alignment", self.client_uid)
            return super().update_segments(segments, duration)
        confirmed, remaining = decision
        if confirmed:
            text = "".join(word.text for word in confirmed)
            completed = self.format_segment(confirmed[0].start, confirmed[-1].end,
                                            text, completed=True, finalization_reason="stable")
            self.text.append(text)
            self.transcript.append(completed)
            if self.translation_queue is not None:
                try:
                    self.translation_queue.put(completed.copy(), timeout=0.1)
                except queue.Full:
                    logging.warning("Translation queue is full, skipping segment")
            with self.lock:
                self.timestamp_offset = confirmed[-1].end
            self.prev_out = ""
            self.same_output_count = 0
            self.end_time_for_same_output = None
            logging.info("ASR_PREFIX_COMMIT uid=%s words=%d audio_end=%.3f commit_end=%.3f text=%r",
                         self.client_uid, len(confirmed), offset + duration,
                         self.timestamp_offset, text)
        self.current_out = "".join(word.text for word in remaining)
        if remaining:
            return self.format_segment(remaining[0].start, remaining[-1].end,
                                       self.current_out, completed=False)
        return None

    def _update_retained_prefix(self, segments, duration, policy):
        offset = self.timestamp_offset
        closed_before = policy.committed_end
        decision = policy.observe(segments, offset, duration, self.no_speech_thresh)
        at_limit = self.bounded_window_enabled() and duration >= self.max_uncommitted_audio_s
        if decision is None:
            logging.info("ASR_CONTEXT_BLOCKED uid=%s reason=%s", self.client_uid, policy.blocked_reason)
            if at_limit:
                if self._trim_retained_context(policy, offset, duration):
                    return getattr(self, "_retained_draft", None)
                if not policy.confirmed and policy.committed_end <= offset:
                    self.current_out = ' '.join(s.text.strip() for s in segments
                        if self.get_segment_no_speech_prob(s) <= self.no_speech_thresh)
                    self.force_commit_bounded_window(duration, SimpleNamespace(start=0, end=duration))
                    policy.move_window(self.timestamp_offset)
                    self._retained_draft = None
                    return None
                # Never silently replay closed context, discard uncertain new
                # speech, or grow an unbounded buffer to hide a failed policy.
                self.exit = True
                self.websocket.send(json.dumps({"uid": self.client_uid, "status": "ERROR",
                    "message": "Retained-prefix research stopped: confirmed context could not be matched safely."}))
                self.websocket.close()
                raise PrefixContextLost(policy.blocked_reason)
            return getattr(self, "_retained_draft", None)
        confirmed, remaining = decision
        if confirmed:
            completed = self.format_segment(max(confirmed[0].start, closed_before), confirmed[-1].end,
                ''.join(w.text for w in confirmed), completed=True, finalization_reason='stable')
            self.text.append(completed['text'])
            self.transcript.append(completed)
            if self.translation_queue is not None:
                try:
                    self.translation_queue.put(completed.copy(), timeout=.1)
                except queue.Full:
                    logging.warning("Translation queue is full, skipping segment")
            logging.info("ASR_RETAINED_PREFIX uid=%s words=%d audio_end=%.3f commit_end=%.3f text=%r",
                         self.client_uid, len(confirmed), offset + duration, confirmed[-1].end, completed['text'])
        self.current_out = ''.join(w.text for w in remaining)
        draft_start = max(remaining[0].start, policy.committed_end) if remaining else offset + duration
        draft_end = max(draft_start, remaining[-1].end) if remaining else draft_start
        self._retained_draft = (self.format_segment(draft_start, draft_end, self.current_out, completed=False)
                                if remaining else None)
        if at_limit:
            if not self._trim_retained_context(policy, offset, duration):
                # The original safety path is still provisional, but receives
                # only the unconfirmed tail, never the already closed prefix.
                tail = SimpleNamespace(start=draft_start - offset, end=draft_end - offset)
                self.force_commit_bounded_window(duration, tail)
                policy.move_window(self.timestamp_offset)
                self._retained_draft = None
                return None
        return self._retained_draft

    def _trim_retained_context(self, policy, offset, duration):
        cut = policy.trim_point(offset + duration, self.context_overlap_s)
        minimum = offset + max(.1, duration - self.max_uncommitted_audio_s + 1)
        if cut is None or cut < minimum:
            return False
        with self.lock:
            self.timestamp_offset = cut
        policy.move_window(cut)
        logging.info("ASR_CONTEXT_TRIM uid=%s offset_before=%.3f offset_after=%.3f retained_s=%.3f",
                     self.client_uid, offset, cut, offset + duration - cut)
        return True

    def handle_transcription_output(self, result, duration):
        """
        Handle the transcription output, updating the transcript and sending data to the client.

        Args:
            result (str): The result from whisper inference i.e. the list of segments.
            duration (float): Duration of the transcribed audio chunk.
        """
        if isinstance(getattr(self, 'stable_prefix', None), FastTextPrefix):
            return self._handle_fast_prefix(result, duration)
        segments = []
        if len(result):
            self.t_start = None
            last_segment = self.update_segments(result, duration)
            segments = self.prepare_segments(last_segment)

        if len(segments):
            self.send_transcription_to_client(segments)
