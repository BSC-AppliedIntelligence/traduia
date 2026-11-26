"""Experimental local agreement over aligned words; no audio-specific rules.

Agreement is evidence of stability, not confidence or correctness. Native segment
timestamps from the BSC checkpoint are deliberately not used for audio trimming.
"""
from dataclasses import dataclass
import math
import unicodedata


BSC_MODEL = "BSC-LT/faster-whisper-large-v3-ca-punctuated-3370h"


@dataclass(frozen=True)
class AlignedWord:
    text: str
    start: float
    end: float

    @property
    def key(self):
        # Keep punctuation: agreement must not invent or discard punctuation.
        return unicodedata.normalize("NFC", self.text.strip()).casefold()


def aligned_words(segments, offset, duration, no_speech_thresh, check_speech=True):
    words = []
    for segment in segments:
        if check_speech and getattr(segment, "no_speech_prob", 0) > no_speech_thresh:
            # Never skip a rejected middle segment and then trim past it.
            return None
        aligned = getattr(segment, "words", None)
        if not aligned or "".join(w.word for w in aligned).strip() != segment.text.strip():
            return None
        segment_words = []
        for word in aligned:
            start, end = float(word.start), float(word.end)
            if (not word.word.strip() or not math.isfinite(start + end)
                    or start < 0 or end < start or end > duration + 0.02
                    or (segment_words and offset + start < segment_words[-1].end - 0.02)):
                return None
            # The aligner can split s'intuïa into " s" and "'intuïa".
            # Only whitespace boundaries are eligible commit boundaries.
            if segment_words and not word.word[0].isspace():
                before = segment_words.pop()
                segment_words.append(AlignedWord(before.text + word.word, before.start,
                                                 offset + min(end, duration)))
            else:
                segment_words.append(AlignedWord(word.word, offset + start, offset + min(end, duration)))
        if words and segment_words:
            if segment_words[0].start < words[-1].end - 0.02:
                return None
            if not segment_words[0].text[0].isspace():
                first = segment_words[0]
                segment_words[0] = AlignedWord(' ' + first.text, first.start, first.end)
        words.extend(segment_words)
    if not words:
        return None
    return words


class StableWordPrefix:
    def __init__(self, hold_words=2, hold_audio_s=0.5, min_new_audio_s=0.25):
        self.hold_words = hold_words
        self.hold_audio_s = hold_audio_s
        self.min_new_audio_s = min_new_audio_s
        self.reset()

    def reset(self):
        self.previous = []
        self.previous_audio_end = None
        self.expected_offset = None

    def observe(self, segments, offset, duration, no_speech_thresh):
        """Return (confirmed words, remaining words), or None for unsafe alignment.

        Two different, growing audio snapshots must agree. Keep two trailing
        words and at least 500 ms of audio provisional. After a commit, compare
        the previous uncommitted suffix to the next window, using absolute times.
        """
        if self.expected_offset is not None and abs(offset - self.expected_offset) > 0.002:
            self.reset()  # A safety reset or buffer eviction invalidates agreement.
        words = aligned_words(segments, offset, duration, no_speech_thresh)
        if words is None:
            self.reset()
            return None
        audio_end = offset + duration
        if self.previous_audio_end is not None and audio_end < self.previous_audio_end + self.min_new_audio_s:
            return [], words  # Re-decoding identical audio is not independent agreement.
        count = 0
        for before, now in zip(self.previous, words):
            if before.key != now.key:
                break
            count += 1
        count = min(count, max(0, len(words) - self.hold_words))
        # Zero-duration tail words occur frequently on unfinished audio. Keep
        # them draft; never use them as evidence to advance past uncertain audio.
        for index, word in enumerate(words[:count]):
            if word.end <= word.start:
                count = index
                break
        while count and words[count - 1].end > audio_end - self.hold_audio_s:
            count -= 1
        # Do not split a group with overlapping word alignment.
        while count and count < len(words) and words[count].start < words[count - 1].end:
            count -= 1
        confirmed, remaining = words[:count], words[count:]
        self.previous = remaining
        self.previous_audio_end = audio_end
        self.expected_offset = confirmed[-1].end if confirmed else offset
        return confirmed, remaining


def anchor_key(word):
    """Ignore outer punctuation only when locating already confirmed context.

    New words must still agree including punctuation. This never changes text.
    """
    text = word.key
    while text and unicodedata.category(text[0]).startswith('P'):
        text = text[1:]
    while text and unicodedata.category(text[-1]).startswith('P'):
        text = text[:-1]
    return text


class PrefixContextLost(RuntimeError):
    """A research stream cannot safely trim without replaying confirmed text."""


class RetainedWordPrefix:
    """Confirm text while keeping audio; trim only at the window limit.

    Confirmed words are tracked separately from the previous unconfirmed tail.
    Re-decoded context is consumed by identity/position, never emitted twice.
    """
    def __init__(self, hold_words=2, hold_audio_s=0.5, min_new_audio_s=0.25, joint_speech_gate=False):
        self.hold_words = hold_words
        self.hold_audio_s = hold_audio_s
        self.min_new_audio_s = min_new_audio_s
        self.joint_speech_gate = joint_speech_gate
        self.confirmed = []
        self.committed_end = 0.0
        self.offset = 0.0
        self.previous = []
        self.previous_audio_end = None
        self.blocked_reason = ''
        self.latest_words = []

    def move_window(self, offset):
        self.confirmed = [w for w in self.confirmed if w.end > offset + 0.001]
        self.offset = offset
        self.previous = []
        self.previous_audio_end = None
        self.latest_words = []

    def _confirmed_prefix_length(self, words):
        if not self.confirmed:
            return 0
        n = len(self.confirmed)
        old_keys = [anchor_key(w) for w in self.confirmed]
        new_keys = [anchor_key(w) for w in words]
        if (len(words) >= n and new_keys[:n] == old_keys
                and abs(words[n - 1].end - self.confirmed[-1].end) <= .75
                and min(words[n - 1].end, self.confirmed[-1].end)
                    > max(words[n - 1].start, self.confirmed[-1].start)):
            return n
        # Cropping can omit a leading context word or prepend an article. A
        # shorter suffix is usable only with a tighter, per-word time match;
        # text similarity alone must never suppress a new spoken repetition.
        for size in range(min(5, n), 0, -1):
            if any(not key for key in old_keys[-size:]):
                continue
            tolerance = .25 if size < 3 else .75
            candidates = [i + size for i in range(len(words) - size + 1)
                          if new_keys[i:i + size] == old_keys[-size:]
                          and all(abs(words[i + j].end - self.confirmed[-size + j].end) <= tolerance
                                  and min(words[i + j].end, self.confirmed[-size + j].end)
                                      > max(words[i + j].start, self.confirmed[-size + j].start)
                                  for j in range(size))]
            if len(candidates) == 1:
                return candidates[0]
            if candidates:
                return None
        return None

    def observe(self, segments, offset, duration, no_speech_thresh):
        if abs(offset - self.offset) > .002:
            self.move_window(offset)
        # A rejected tail must not throw away preceding recognized speech.
        # Stop at the first rejected segment, including a rejected middle:
        # never trim past that gap on evidence from later text.
        leading = []
        for segment in segments:
            rejected = getattr(segment, 'no_speech_prob', 0) > no_speech_thresh
            logprob = getattr(segment, 'avg_logprob', None)
            # Optional comparison arm: retain the existing probability cutoff,
            # adding the deployed decoder's log-probability safeguard. This is
            # speech filtering, not choosing among competing text revisions.
            if self.joint_speech_gate and logprob is not None and logprob > -1.0:
                rejected = False
            if rejected:
                break
            leading.append(segment)
        words = aligned_words(leading, offset, duration, no_speech_thresh, check_speech=False)
        prefix = self._confirmed_prefix_length(words) if words else None
        if prefix is None:
            self.blocked_reason = 'unsafe_alignment' if words is None else 'confirmed_context_mismatch'
            self.previous = []
            self.previous_audio_end = None
            return None
        self.blocked_reason = ''
        # Refresh aligned positions of retained context, without changing any
        # text that has already entered the transcript/translation queue.
        self.confirmed = words[:prefix]
        self.latest_words = words
        tail = words[prefix:]
        audio_end = offset + duration
        if self.previous_audio_end is not None and audio_end < self.previous_audio_end + self.min_new_audio_s:
            return [], tail
        count = 0
        for before, now in zip(self.previous, tail):
            if before.key != now.key:
                break
            count += 1
        count = min(count, max(0, len(tail) - self.hold_words))
        for i, word in enumerate(tail[:count]):
            if word.end <= word.start or word.end <= self.committed_end:
                count = i
                break
        while count and tail[count - 1].end > audio_end - self.hold_audio_s:
            count -= 1
        while count and count < len(tail) and tail[count].start < tail[count - 1].end:
            count -= 1
        confirmed, remaining = tail[:count], tail[count:]
        self.confirmed.extend(confirmed)
        self.previous = remaining
        self.previous_audio_end = audio_end
        if confirmed:
            self.committed_end = confirmed[-1].end
        return confirmed, remaining

    def trim_point(self, audio_end, overlap_s):
        """Choose a confirmed word boundary while retaining decoder context."""
        target = audio_end - max(overlap_s, self.hold_audio_s)
        candidates = []
        for i, word in enumerate(self.confirmed):
            following = (self.latest_words[i + 1] if i + 1 < len(self.latest_words) else
                         self.confirmed[i + 1] if i + 1 < len(self.confirmed) else None)
            if (word.end > self.offset + .1 and word.end <= target and word.end > word.start
                    and (following is None or word.end <= following.start)):
                candidates.append(word.end)
        return max(candidates) if candidates else None
