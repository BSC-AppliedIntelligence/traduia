import json
import logging
import threading
import time
import queue
import numpy as np


class ServeClientBase(object):
    RATE = 16000
    SERVER_READY = "SERVER_READY"
    DISCONNECT = "DISCONNECT"

    client_uid: str
    """A unique identifier for the client."""
    websocket: object
    """The WebSocket connection for the client."""
    send_last_n_segments: int
    """Number of most recent segments to send to the client."""
    no_speech_thresh: float
    """Segments with no speech probability above this threshold will be discarded."""
    clip_audio: bool
    """Whether to clip audio with no valid segments."""
    same_output_threshold: int
    """Number of repeated outputs before considering it as a valid segment."""

    def __init__(
        self,
        client_uid,
        websocket,
        send_last_n_segments=10,
        no_speech_thresh=0.45,
        clip_audio=False,
        same_output_threshold=10,
        translation_queue=None,
        max_uncommitted_audio_s=0.0,
        context_overlap_s=1.0,
        latency_logging=False,
    ):
        self.client_uid = client_uid
        self.websocket = websocket
        self.send_last_n_segments = send_last_n_segments
        self.no_speech_thresh = no_speech_thresh
        self.clip_audio = clip_audio
        self.same_output_threshold = same_output_threshold
        self.max_uncommitted_audio_s = max(0.0, float(max_uncommitted_audio_s or 0.0))
        self.context_overlap_s = max(0.0, float(context_overlap_s or 0.0))
        self.latency_logging = bool(latency_logging)

        self.frames = b""
        self.timestamp_offset = 0.0
        self.frames_np = None
        self.frames_offset = 0.0
        self.text = []
        self.current_out = ""
        self.prev_out = ""
        self.exit = False
        self.same_output_count = 0
        self.transcript = []
        self.end_time_for_same_output = None
        self.translation_queue = translation_queue

        # threading
        self.lock = threading.Lock()

    def speech_to_text(self):
        """
        Process an audio stream in an infinite loop, continuously transcribing the speech.

        This method continuously receives audio frames, performs real-time transcription, and sends
        transcribed segments to the client via a WebSocket connection.

        If the client's language is not detected, it waits for 30 seconds of audio input to make a language prediction.
        It utilizes the Whisper ASR model to transcribe the audio, continuously processing and streaming results. Segments
        are sent to the client in real-time, and a history of segments is maintained to provide context.

        Raises:
            Exception: If there is an issue with audio processing or WebSocket communication.

        """
        while True:
            if self.exit:
                logging.info("Exiting speech to text thread")
                break

            if self.frames_np is None:
                continue

            if self.clip_audio:
                self.clip_audio_if_no_valid_segment()

            input_bytes, duration = self.get_audio_chunk_for_processing()
            self.log_asr_window(duration)
            if duration < 1.0:
                time.sleep(0.1)     # wait for audio chunks to arrive
                continue
            try:
                input_sample = input_bytes.copy()
                result = self.transcribe_audio(input_sample)

                if result is None or self.language is None:
                    self.timestamp_offset += duration
                    time.sleep(0.25)    # wait for voice activity, result is None when no voice activity
                    continue
                self.handle_transcription_output(result, duration)

            except Exception as e:
                logging.error(f"[ERROR]: Failed to transcribe audio chunk: {e}")
                time.sleep(0.01)

    def transcribe_audio(self):
        raise NotImplementedError

    def handle_transcription_output(self, result, duration):
        raise NotImplementedError
    
    def format_segment(self, start, end, text, completed=False, finalization_reason=""):
        """
        Formats a transcription segment with precise start and end times alongside the transcribed text.

        Args:
            start (float): The start time of the transcription segment in seconds.
            end (float): The end time of the transcription segment in seconds.
            text (str): The transcribed text corresponding to the segment.

        Returns:
            dict: A dictionary representing the formatted transcription segment, including
                'start' and 'end' times as strings with three decimal places and the 'text'
                of the transcription.
        """
        segment = {
            'start': "{:.3f}".format(start),
            'end': "{:.3f}".format(end),
            'text': text,
            'completed': completed
        }
        if finalization_reason:
            segment['finalization_reason'] = finalization_reason
        return segment

    def add_frames(self, frame_np):
        """
        Add audio frames to the ongoing audio stream buffer.

        This method is responsible for maintaining the audio stream buffer, allowing the continuous addition
        of audio frames as they are received. It also ensures that the buffer does not exceed a specified size
        to prevent excessive memory usage.

        If the buffer size exceeds a threshold (45 seconds of audio data), it normally discards the oldest
        30 seconds. A retained-context policy may limit this to already retired samples.
        If the buffer is empty, it initializes it with the provided
        audio frame. The audio stream buffer is used for real-time processing of audio data for transcription.

        Args:
            frame_np (numpy.ndarray): The audio frame data as a NumPy array.

        """
        with self.lock:
            if self.frames_np is not None and self.frames_np.shape[0] > 45*self.RATE:
                trim_samples = self._buffer_trim_samples()
                self.frames_offset += trim_samples / self.RATE
                self.frames_np = self.frames_np[trim_samples:]
                if self.timestamp_offset < self.frames_offset:
                    self.timestamp_offset = self.frames_offset
            if self.frames_np is None:
                self.frames_np = frame_np.copy()
            else:
                self.frames_np = np.concatenate((self.frames_np, frame_np), axis=0)

    def _buffer_trim_samples(self):
        """Return the allowed eviction under the audio lock; legacy defaults to 30 s."""
        return 30 * self.RATE

    def clip_audio_if_no_valid_segment(self):
        """
        Update the timestamp offset based on audio buffer status.
        Clip audio if the current chunk exceeds 30 seconds, this basically implies that
        no valid segment for the last 30 seconds from whisper
        """
        with self.lock:
            if self.frames_np[int((self.timestamp_offset - self.frames_offset)*self.RATE):].shape[0] > 25 * self.RATE:
                duration = self.frames_np.shape[0] / self.RATE
                self.timestamp_offset = self.frames_offset + duration - 5

    def bounded_window_enabled(self):
        return self.max_uncommitted_audio_s > 0

    def log_asr_window(self, duration):
        if not self.latency_logging:
            return
        max_uncommitted = (
            f"{self.max_uncommitted_audio_s:.3f}"
            if self.bounded_window_enabled()
            else "disabled"
        )
        logging.info(
            "ASR_WINDOW uid=%s input_s=%.3f max_uncommitted_s=%s "
            "timestamp_offset=%.3f frames_offset=%.3f",
            self.client_uid,
            duration,
            max_uncommitted,
            self.timestamp_offset,
            self.frames_offset,
        )

    def force_commit_bounded_window(self, duration, segment):
        if not self.bounded_window_enabled() or duration < self.max_uncommitted_audio_s:
            return False

        text = self.current_out.strip()
        segment_start = max(0.0, self.get_segment_start(segment))
        segment_end = max(segment_start, self.get_segment_end(segment))
        commit_offset = min(duration, segment_end)

        # A bounded-window reset must be based on the audio-buffer duration,
        # not solely on model timestamps. Some models return a long result
        # with a stale segment end, and a no-speech result can leave text
        # empty. In both cases, timestamp-based progress alone allows the
        # supposedly bounded window to keep growing.
        effective_overlap = min(
            self.context_overlap_s,
            max(0.0, duration - 0.1),
        )
        required_advance = max(0.0, duration - effective_overlap)
        timestamp_advance = max(0.0, commit_offset - effective_overlap)
        advance = max(required_advance, timestamp_advance)

        with self.lock:
            offset_before = self.timestamp_offset
            if text:
                # Never leave a gap between the finalized timeline and the
                # retained overlap when model timestamps lag behind the audio.
                completed_end = max(commit_offset, advance)
                completed_segment = self.format_segment(
                    self.timestamp_offset + min(segment_start, completed_end),
                    self.timestamp_offset + completed_end,
                    text,
                    completed=True,
                    finalization_reason="bounded_window",
                )
                if not self.text or self.text[-1].strip().lower() != text.lower():
                    self.text.append(text)
                    self.transcript.append(completed_segment)
                    if self.translation_queue:
                        try:
                            self.translation_queue.put(completed_segment.copy(), timeout=0.1)
                        except queue.Full:
                            logging.warning("Translation queue is full, skipping segment")

            self.timestamp_offset += advance
            offset_after = self.timestamp_offset

        if self.latency_logging:
            logging.info(
                "ASR_BOUNDARY_COMMIT uid=%s reason=max_uncommitted "
                "window_s=%.3f commit_end=%.3f offset_before=%.3f "
                "offset_after=%.3f overlap_s=%.3f text=%r",
                self.client_uid,
                duration,
                offset_before + max(commit_offset, advance),
                offset_before,
                offset_after,
                effective_overlap,
                text,
            )

        self.current_out = ''
        self.prev_out = ''
        self.same_output_count = 0
        self.end_time_for_same_output = None
        return True

    def get_audio_chunk_for_processing(self):
        """
        Retrieves the next chunk of audio data for processing based on the current offsets.

        Calculates which part of the audio data should be processed next, based on
        the difference between the current timestamp offset and the frame's offset, scaled by
        the audio sample rate (RATE). It then returns this chunk of audio data along with its
        duration in seconds.

        Returns:
            tuple: A tuple containing:
                - input_bytes (np.ndarray): The next chunk of audio data to be processed.
                - duration (float): The duration of the audio chunk in seconds.
        """
        with self.lock:
            samples_take = max(0, (self.timestamp_offset - self.frames_offset) * self.RATE)
            input_bytes = self.frames_np[int(samples_take):].copy()
        duration = input_bytes.shape[0] / self.RATE
        return input_bytes, duration

    def prepare_segments(self, last_segment=None):
        """
        Prepares the segments of transcribed text to be sent to the client.

        This method compiles the recent segments of transcribed text, ensuring that only the
        specified number of the most recent segments are included. It also appends the most
        recent segment of text if provided (which is considered incomplete because of the possibility
        of the last word being truncated in the audio chunk).

        Args:
            last_segment (str, optional): The most recent segment of transcribed text to be added
                                          to the list of segments. Defaults to None.

        Returns:
            list: A list of transcribed text segments to be sent to the client.
        """
        segments = []
        if len(self.transcript) >= self.send_last_n_segments:
            segments = self.transcript[-self.send_last_n_segments:].copy()
        else:
            segments = self.transcript.copy()
        if last_segment is not None:
            segments = segments + [last_segment]
        return segments

    def get_audio_chunk_duration(self, input_bytes):
        """
        Calculates the duration of the provided audio chunk.

        Args:
            input_bytes (numpy.ndarray): The audio chunk for which to calculate the duration.

        Returns:
            float: The duration of the audio chunk in seconds.
        """
        return input_bytes.shape[0] / self.RATE

    def send_transcription_to_client(self, segments):
        """
        Sends the specified transcription segments to the client over the websocket connection.

        This method formats the transcription segments into a JSON object and attempts to send
        this object to the client. If an error occurs during the send operation, it logs the error.

        Returns:
            segments (list): A list of transcription segments to be sent to the client.
        """
        try:
            self.websocket.send(
                json.dumps({
                    "uid": self.client_uid,
                    "segments": segments,
                })
            )
        except Exception as e:
            logging.error(f"[ERROR]: Sending data to client: {e}")

    def disconnect(self):
        """
        Notify the client of disconnection and send a disconnect message.

        This method sends a disconnect message to the client via the WebSocket connection to notify them
        that the transcription service is disconnecting gracefully.

        """
        self.websocket.send(json.dumps({
            "uid": self.client_uid,
            "message": self.DISCONNECT
        }))

    def cleanup(self):
        """
        Perform cleanup tasks before exiting the transcription service.

        This method performs necessary cleanup tasks, including stopping the transcription thread, marking
        the exit flag to indicate the transcription thread should exit gracefully, and destroying resources
        associated with the transcription process.

        """
        logging.info("Cleaning up.")
        self.exit = True
    
    def get_segment_no_speech_prob(self, segment):
        return getattr(segment, "no_speech_prob", 0)

    def get_segment_start(self, segment):
        return getattr(segment, "start", getattr(segment, "start_ts", 0))

    def get_segment_end(self, segment):
        return getattr(segment, "end", getattr(segment, "end_ts", 0))

    def update_segments(self, segments, duration):
        """
        Processes the segments from Whisper and updates the transcript.
        Uses helper methods to account for differences between backends.
        
        Args:
            segments (list): List of segments returned by the transcriber.
            duration (float): Duration of the current audio chunk.
        
        Returns:
            dict or None: The last processed segment (if any).
        """
        offset = None
        self.current_out = ''
        last_segment = None
        accepted_completed = 0

        # Each segment has its own speech confidence. A rejected trailing
        # segment must not discard valid earlier speech in the same window.
        if len(segments) > 1:
            for s in segments[:-1]:
                text_ = s.text
                with self.lock:
                    start = self.timestamp_offset + self.get_segment_start(s)
                    end = self.timestamp_offset + min(duration, self.get_segment_end(s))
                if start >= end:
                    continue
                if self.get_segment_no_speech_prob(s) > self.no_speech_thresh:
                    continue
                self.text.append(text_)
                completed_segment = self.format_segment(start, end, text_, completed=True)
                self.transcript.append(completed_segment)
                accepted_completed += 1

                if self.translation_queue:
                    try:
                        self.translation_queue.put(completed_segment.copy(), timeout=0.1)
                    except queue.Full:
                        logging.warning("Translation queue is full, skipping segment")
                offset = min(duration, self.get_segment_end(s))

        # Process the last segment if its no_speech_prob is acceptable.
        if self.get_segment_no_speech_prob(segments[-1]) <= self.no_speech_thresh:
            self.current_out += segments[-1].text
            with self.lock:
                last_segment = self.format_segment(
                    self.timestamp_offset + self.get_segment_start(segments[-1]),
                    self.timestamp_offset + min(duration, self.get_segment_end(segments[-1])),
                    self.current_out,
                    completed=False
                )
        elif accepted_completed and self.latency_logging:
            logging.info(
                "ASR_VALID_PREFIX uid=%s accepted_completed=%d rejected_tail_no_speech=%.3f",
                self.client_uid, accepted_completed, self.get_segment_no_speech_prob(segments[-1]),
            )

        if self.force_commit_bounded_window(duration, segments[-1]):
            return None

        # Handle repeated output logic.
        if self.current_out.strip() == self.prev_out.strip() and self.current_out != '':
            self.same_output_count += 1

            # if we remove the audio because of same output on the nth reptition we might remove the 
            # audio thats not yet transcribed so, capturing the time when it was repeated for the first time
            if self.end_time_for_same_output is None:
                self.end_time_for_same_output = self.get_segment_end(segments[-1])
            time.sleep(0.1)  # wait briefly for any new voice activity
        else:
            self.same_output_count = 0
            self.end_time_for_same_output = None

        # If the same incomplete segment is repeated too many times,
        # append it to the transcript and update the offset.
        if self.same_output_count > self.same_output_threshold:
            if not self.text or self.text[-1].strip().lower() != self.current_out.strip().lower():
                self.text.append(self.current_out)
                with self.lock:
                    completed_segment = self.format_segment(
                        self.timestamp_offset,
                        self.timestamp_offset + min(duration, self.end_time_for_same_output),
                        self.current_out,
                        completed=True
                    )
                    self.transcript.append(completed_segment)

                    if self.translation_queue:
                        try:
                            self.translation_queue.put(completed_segment.copy(), timeout=0.1)
                        except queue.Full:
                            logging.warning("Translation queue is full, skipping segment")

            self.current_out = ''
            offset = min(duration, self.end_time_for_same_output)
            self.same_output_count = 0
            last_segment = None
            self.end_time_for_same_output = None
        else:
            self.prev_out = self.current_out

        if offset is not None:
            with self.lock:
                self.timestamp_offset += offset

        return last_segment
