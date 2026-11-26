"""Opt-in text agreement, with acoustic alignment only for context recovery.

Text IDs describe delivery order, never acoustic times. Published prefixes are
immutable; retained decoder context may be corrected without republishing it.
"""
import logging
import math
import re
import unicodedata
from difflib import SequenceMatcher
from types import SimpleNamespace

from whisper_live.stable_prefix import RetainedWordPrefix, anchor_key, aligned_words, PrefixContextLost

PROTOCOL = "stable_text_prefix_v1"


def words(text):
    return re.findall(r"\s*\S+", text)


def keys(items):
    return [anchor_key(SimpleNamespace(key=unicodedata.normalize('NFC', w.strip()).casefold())) for w in items]


def accepted_text(segments, threshold):
    leading = []
    for segment in segments:
        if (getattr(segment, "no_speech_prob", 0) > threshold
                and getattr(segment, "avg_logprob", -float("inf")) <= -1):
            break  # Never skip a rejected middle segment.
        leading.append(segment.text)
    return words(" ".join(t.strip() for t in leading))


def align_text(model, samples, text, language, offset=0.):
    """Align existing text to raw audio; never perform another text decode.

    Raw audio keeps the original clock (including silence). No native BSC
    segment timestamp is used. Keep the same validation as the aligned policy.
    """
    from faster_whisper.audio import pad_or_trim
    from faster_whisper.tokenizer import Tokenizer
    duration = len(samples) / model.feature_extractor.sampling_rate
    if not text.strip() or not 0 < duration <= 25:
        raise PrefixContextLost('forced alignment requires nonempty text and bounded audio')
    tokenizer = Tokenizer(model.hf_tokenizer, model.model.is_multilingual,
                          language=language, task='transcribe')
    tokens = tokenizer.encode(' ' + text.strip())
    if len(tokens) + len(tokenizer.sot_sequence) + 1 > model.max_length:
        raise PrefixContextLost('forced alignment text exceeds model token limit')
    features = model.feature_extractor(samples)
    frames = min(features.shape[-1] - 1, model.feature_extractor.nb_max_frames)
    encoded = model.encode(pad_or_trim(features[:, :frames]))
    result = model.find_alignment(tokenizer, [tokens], encoded, frames)
    items = result[0] if result else []
    segment = SimpleNamespace(text=text, words=[SimpleNamespace(
        word=w['word'], start=w['start'], end=w['end']) for w in items])
    aligned = aligned_words([segment], offset, duration, 1., check_speech=False)
    if aligned is None:
        raise PrefixContextLost('forced alignment is empty or inconsistent with decoded text')
    return aligned


class FastTextPrefix:
    def __init__(self, hold_words=2, min_new_audio_s=.5, max_recovery_span_s=5.):
        self.hold_words = hold_words
        self.min_new_audio_s = min_new_audio_s
        self.max_recovery_span_s = max_recovery_span_s
        self.confirmed = []
        self.timed = []
        self.previous = []
        self.previous_end = None
        self.sequence = 0
        self.update_sequence = 0
        self.history = []
        self.draft = ""
        self._short_recovery = None
        self._rewrite_recovery = None
        self._first_short_word = None
        self._acoustic_recovery = None
        self._trim_evidence = None

    def recover_closed_audio(self, current, audio_end, *, offset=None, overlap_s=None):
        """Reconcile rewritten *closed* audio using two checked alignments.

        Published history is independent of the decoder's retained wording.
        Only a contiguous prefix wholly inside the previously closed acoustic
        span can replace that wording. A word crossing its end remains pending.
        Fresh, growing snapshots must agree on both text and the audio boundary;
        one alignment, zero-duration words or a later repeated phrase cannot
        authorize this recovery. No audio is removed here.
        """
        old = self.timed
        if (len(old) != len(self.confirmed) or len(old) < 2
                or not math.isfinite(audio_end)
                or any(not math.isfinite(w.start + w.end) or w.end <= w.start for w in old)
                or old[0].start < 0 or old[-1].end > audio_end + .02
                or not .5 <= old[-1].end - old[0].start <= self.max_recovery_span_s):
            self._acoustic_recovery = None
            return None
        start, end = old[0].start, old[-1].end
        count = 0
        for word in current:
            if (not math.isfinite(word.start + word.end) or word.end < word.start
                    or word.start < start - .1 or word.end > end + .02
                    or (count and word.start < current[count - 1].end - .02)):
                break
            count += 1
        if (count < 2 or count >= len(current)
                or sum(w.end > w.start for w in current[:count]) < 2
                or current[count - 1].end <= current[count - 1].start
                or current[count].start < current[count - 1].end
                or current[0].start > start + .1
                or end - current[count - 1].end > .15):
            self._acoustic_recovery = None
            return None
        signature = (tuple(keys(self.confirmed)), tuple(keys([w.text for w in current[:count]])))
        boundary = current[count - 1].end
        previous = self._acoustic_recovery
        self._acoustic_recovery = (signature, audio_end, boundary, list(current))
        if (previous is None or previous[0] != signature
                or abs(previous[2] - boundary) > .08
                or audio_end < previous[1] + self.min_new_audio_s):
            # Do not move the clock forward on repeated observations of the
            # same audio; a later genuinely growing snapshot must still agree.
            if (previous is not None and previous[0] == signature
                    and abs(previous[2] - boundary) <= .08 and audio_end >= previous[1]):
                self._acoustic_recovery = previous
            return None
        self.confirmed = [w.text for w in current[:count]]
        self.timed = current[:count]
        self.previous = [w.text for w in previous[3][count:]]
        self.previous_end = previous[1]
        self._short_recovery = self._rewrite_recovery = self._acoustic_recovery = None
        self._trim_evidence = None
        if offset is not None and overlap_s is not None:
            # Recovery already has two growing alignments. Retain the first
            # as trimming evidence instead of unnecessarily waiting for a third
            # snapshot near the safety limit. Inner boundaries are still checked
            # independently by checked_short_trim on the current alignment.
            self.checked_short_trim(previous[3], offset, previous[1], overlap_s)
        logging.info('ASR_FAST_AUDIO_RECOVERY words=%d through_s=%.3f', count, boundary)
        return count

    def checked_short_trim(self, aligned, offset, audio_end, overlap_s):
        """Keep an acoustic overlap when four lexical context words do not fit.

        This is a fallback for sparse confirmed text, never permission to trim
        a draft. At least one whole confirmed word stays as decoder context.
        The boundary and retained word must have positive, agreeing alignment
        on two growing snapshots. Ordinary four-word-context trimming remains
        the preferred path.
        """
        n = len(self.confirmed)
        if (n < 2 or n > len(aligned) or self.prefix_length([w.text for w in aligned]) != n
                or not math.isfinite(offset + audio_end + overlap_s)):
            self._trim_evidence = None
            return None
        candidates = []
        for i in range(n - 1):
            left, right = aligned[i:i + 2]
            if (all(math.isfinite(w.start + w.end) and w.end > w.start for w in [left, right])
                    and offset + self.min_new_audio_s <= left.end <= audio_end - overlap_s
                    and left.end <= right.start
                    and right.end - left.end >= .25):
                candidates.append((left.end, right.start, right.end))
        previous = self._trim_evidence
        self._trim_evidence = (offset, audio_end, candidates)
        if previous is None or previous[0] != offset:
            return None
        if audio_end < previous[1] + self.min_new_audio_s:
            if audio_end >= previous[1] and candidates == previous[2]:
                self._trim_evidence = previous
            return None
        supported = [now[0] for now in candidates if any(
            all(abs(a - b) <= .08 for a, b in zip(now, before)) for before in previous[2])]
        return max(supported) if supported else None

    def prefix_length(self, items):
        n = len(self.confirmed)
        return n if keys(items[:n]) == keys(self.confirmed) and len(items) >= n else None

    def align_context(self, aligned):
        texts = [w.text for w in aligned]
        count = self.prefix_length(texts)
        if count is None and len(self.timed) == len(self.confirmed):
            matcher = RetainedWordPrefix()
            matcher.confirmed = self.timed
            count = matcher._confirmed_prefix_length(aligned)
            if count is None:
                # Alignment can collapse one short word to zero duration.
                # A lone such word cannot locate context, but a unique phrase
                # with at least two genuinely overlapping neighbours can.
                old_keys, new_keys = keys(self.confirmed), keys(texts)
                for size in range(min(5, len(self.timed)), 2, -1):
                    anchor = old_keys[-size:]
                    if any(not k for k in anchor) or len(set(anchor)) < 2:
                        continue
                    candidates = []
                    for i in range(len(aligned) - size + 1):
                        if new_keys[i:i + size] != anchor:
                            continue
                        pairs = list(zip(self.timed[-size:], aligned[i:i + size]))
                        overlaps = [min(a.end, b.end) > max(a.start, b.start) for a, b in pairs]
                        valid = all(abs(a.end - b.end) <= .25 and
                            (overlap or ((a.end == a.start or b.end == b.start)
                                         and abs(a.end - b.end) <= .08))
                            for (a, b), overlap in zip(pairs, overlaps))
                        if valid and sum(overlaps) >= 2:
                            candidates.append(i + size)
                    if len(candidates) == 1:
                        count = candidates[0]
                        break
                    if candidates:
                        break  # Ambiguous repeated anchors never authorize a trim.
            if count is None:
                count = self._corrected_context_boundary(aligned)
        if count is None:
            return None
        self._short_recovery = None
        self._acoustic_recovery = None
        self.confirmed = texts[:count]
        self.timed = aligned[:count]
        return count

    def _corrected_context_boundary(self, aligned):
        """Locate a short rewritten closed tail using text AND shared audio.

        A later hypothesis may contract an earlier hallucinated repetition or
        change a closed word. Do not republish/revise that history, and do not
        confuse it with new speech. Require a unique multiword timed anchor,
        a short edit beside it and majority overlap with already closed audio.
        """
        if not self.timed or len(self.timed) != len(self.confirmed):
            return None
        old, new = keys(self.confirmed), keys([w.text for w in aligned])
        closed_start, closed_end = self.timed[0].start, self.timed[-1].end
        boundaries = []
        for i, word in enumerate(aligned):
            duration = word.end - word.start
            overlap = min(word.end, closed_end) - max(word.start, closed_start)
            if (duration > 0 and overlap / duration >= .5
                    and word.end <= closed_end + .75
                    and any(max(duration, old_word.end-old_word.start) > 0
                        and (min(word.end, old_word.end)-max(word.start, old_word.start))
                            / max(duration, old_word.end-old_word.start) >= .5
                        and abs(word.end-old_word.end) <= .75 for old_word in self.timed)):
                boundaries.append(i + 1)
        if not boundaries:
            return None
        boundary = max(boundaries)
        # Distinctive anchors can be separated by corrected articles or names.
        # They must be unique in both windows, monotonic and refer mostly to
        # the same audio, never merely resemble a phrase spoken later.
        anchors = []
        for i in range(max(0, len(old) - 8), len(old)):
            key = old[i]
            if len(key) < 4 or old.count(key) != 1 or new.count(key) != 1:
                continue
            j = new.index(key)
            a, b = self.timed[i], aligned[j]
            span = max(a.end-a.start, b.end-b.start)
            if (max(0, boundary-8) <= j < boundary and span > 0
                    and (min(a.end, b.end)-max(a.start, b.start)) / span >= .5
                    and abs(a.end-b.end) <= .75):
                anchors.append((i, j, key))
        if (len(anchors) >= 2 and sum(len(k) for _, _, k in anchors) >= 12
                and all(a[1] < b[1] for a, b in zip(anchors, anchors[1:]))
                and closed_end-self.timed[anchors[-1][0]].end <= self.max_recovery_span_s
                and aligned[boundary-1].end-aligned[anchors[-1][1]].end <= self.max_recovery_span_s):
            return boundary
        for block in SequenceMatcher(None, old, new, autojunk=False).get_matching_blocks():
            if block.size < 2 or block.a + block.size > len(old):
                continue
            if block.b + block.size > boundary:
                continue
            if (closed_end-self.timed[block.a+block.size-1].end > self.max_recovery_span_s
                    or aligned[boundary-1].end-aligned[block.b+block.size-1].end > self.max_recovery_span_s):
                continue
            anchor = old[block.a:block.a + block.size]
            if any(not k for k in anchor) or len(' '.join(anchor)) < 12:
                continue
            if any(sum(items[j:j + len(anchor)] == anchor for j in range(len(items)-len(anchor)+1)) != 1
                   for items in [old, new]):
                continue
            pairs = list(zip(self.timed[block.a:block.a + block.size],
                             aligned[block.b:block.b + block.size]))
            # Forced alignment may move an internal word boundary while keeping
            # the whole unique phrase at the same acoustic position. Compare
            # that phrase's outer span, rather than requiring each word to keep
            # its duration. This only recovers a short, already-closed tail.
            first_old, first_new = pairs[0]
            last_old, last_new = pairs[-1]
            span = max(last_old.end-first_old.start, last_new.end-first_new.start)
            overlap = min(last_old.end, last_new.end)-max(first_old.start, first_new.start)
            if (len(set(anchor)) >= 2
                    and block.a >= max(0, len(old)-8) and block.b >= max(0, boundary-8)
                    and sum(a.end > a.start and b.end > b.start for a, b in pairs) >= 2
                    and span > 0 and overlap / span >= .8
                    and abs(first_old.start-first_new.start) <= .25
                    and abs(last_old.end-last_new.end) <= .25):
                logging.info('ASR_FAST_PHRASE_RECOVERY words=%d boundary=%d start=%.3f end=%.3f',
                             block.size, boundary, first_new.start, last_new.end)
                return boundary
            if block.size < 3:
                continue
            support = sum(abs(a.end - b.end) <= .25
                          and min(a.end, b.end) > max(a.start, b.start) for a, b in pairs)
            if support >= max(2, block.size - 1):
                return boundary
        return None

    def recover_short_context(self, old, current, audio_end):
        """Recover a short final-word correction using anchors on both sides.

        The right anchor may still be draft in the saved commit snapshot. It
        locates the correction without consuming that unconfirmed speech.
        Require the same boundary on two growing snapshots, not merely another
        forced alignment of the same audio. Published history remains immutable.
        """
        def overlaps(a, b):
            span = max(a.end-a.start, b.end-b.start)
            return (span > 0 and min(a.end, b.end)-max(a.start, b.start) >= .65*span
                    and abs(a.start-b.start) <= .1 and abs(a.end-b.end) <= .1)

        def candidate():
            n = len(self.confirmed)
            if not (2 <= n <= 4 and len(old) > n and len(current) > n
                    and math.isfinite(audio_end)):
                return None
            old_keys, new_keys = keys([w.text for w in old]), keys([w.text for w in current])
            if old_keys[:n] != keys(self.confirmed):
                return None
            if (not all(math.isfinite(v) for w in old+current for v in (w.start,w.end))
                    or not all(w.end > w.start for w in old[:n])
                    or old[n-1].end-old[0].start > 1.5
                    or old[n-1].end-old[n-1].start > .6):
                return None
            left = old_keys[n-2]
            if len(left) < 4 or old_keys.count(left) != 1 or new_keys.count(left) != 1:
                return None
            shift = new_keys.index(left)-(n-2)
            boundary = n+shift
            if (shift not in (0,1) or boundary >= len(current)
                    or new_keys[shift:boundary-1] != old_keys[:n-1]
                    or new_keys[boundary-1] == old_keys[n-1]):
                return None
            if shift and (current[0].start < old[0].start-.02
                          or current[0].end > old[0].start+.1):
                return None
            if (not all(overlaps(a,b) for a,b in zip(old[:n],current[shift:boundary]))
                    or current[boundary-1].end <= current[boundary-1].start
                    or current[boundary].start < current[boundary-1].end):
                return None
            # A distinctive, uniquely located unconfirmed word must also match
            # in the same nearby audio. Never swallow it to fix the closed tail.
            for i in range(n,len(old)):
                right = old_keys[i]
                if (old[i].start-old[n-1].end > 2. or len(right) < 4
                        or old_keys.count(right) != 1 or new_keys.count(right) != 1):
                    continue
                j = new_keys.index(right)
                if j >= boundary and overlaps(old[i],current[j]):
                    return boundary, (tuple(old_keys[:n]),tuple(new_keys[:boundary]),left,right)
            return None

        found = candidate()
        if found is None:
            self._short_recovery = None
            return None
        boundary, signature = found
        cut = current[boundary-1].end
        previous = self._short_recovery
        if (previous is None or previous[0] != signature or abs(previous[2]-cut) > .08
                or audio_end < previous[1]):
            self._short_recovery = (signature,audio_end,cut)
            return None
        if audio_end < previous[1]+self.min_new_audio_s:
            return None
        self.confirmed = [w.text for w in current[:boundary]]
        self.timed = current[:boundary]
        self._short_recovery = None
        logging.info('ASR_FAST_SHORT_RECOVERY words=%d through_s=%.3f',boundary,cut)
        return boundary

    def reopen_rewritten_context(self, current, audio_end):
        """Resume a bounded rewritten tail without discarding any new speech.

        Alignment-based recovery may suppress corrected *closed* context only
        when it can locate it safely. Otherwise, after fresh text agreement,
        retreat the internal comparison boundary to the first changed word.
        Unlike alignment recovery this consumes NONE of the disputed words.
        It moves no audio clock and never edits published history. A corrected
        fragment can therefore be delivered again; that accuracy cost is safer
        than swallowing pending speech or stopping the stream.
        """
        texts = [w.text for w in current]
        old, new = keys(self.confirmed), keys(texts)
        shared = 0
        for a, b in zip(old, new):
            if a != b:
                break
            shared += 1
        tail = len(old) - shared
        valid = (0 < tail <= 8 and len(new) >= shared + 3
                 and len(self.timed) == len(old) and math.isfinite(audio_end)
                 and all(math.isfinite(v) for w in self.timed for v in (w.start, w.end))
                 and all(w.end >= w.start for w in self.timed)
                 and 0 < self.timed[-1].end-self.timed[shared].start <= self.max_recovery_span_s
                 and all(new[shared:shared+3]))
        if not valid:
            self._rewrite_recovery = None
            return None
        signature = (tuple(old), shared, tuple(new[shared:shared+3]))
        previous = self._rewrite_recovery
        if previous is None or previous[0] != signature or audio_end < previous[1]:
            self._rewrite_recovery = (signature, audio_end, texts)
            return None
        if audio_end < previous[1] + self.min_new_audio_s:
            return None
        self.confirmed = texts[:shared]
        self.timed = current[:shared]
        # Discard the old hypothesis's pending evidence. The *new* candidate
        # was already seen on the first recovery snapshot; ordinary observe()
        # may compare that fresh text (including punctuation) with this second
        # growing snapshot. Do not demand a redundant third observation.
        self.previous, self.previous_end = previous[2][shared:], previous[1]
        self.draft = ''.join(texts[shared:]).strip()
        self._short_recovery = self._rewrite_recovery = None
        self._acoustic_recovery = self._trim_evidence = None
        self._first_short_word = None
        logging.info('ASR_FAST_CONTEXT_REOPEN words=%d retained_words=%d audio_end=%.3f',
                     tail, shared, audio_end)
        return shared

    def observe(self, items, audio_end, quiet=False):
        count = self.prefix_length(items)
        if count is None:
            return None
        self._short_recovery = None
        self._rewrite_recovery = None
        self._acoustic_recovery = None
        tail = items[count:]
        self.draft = "".join(tail).strip()
        if self.previous_end is not None and audio_end < self.previous_end + self.min_new_audio_s:
            return []
        agreed = 0
        for old, new in zip(self.previous, tail):
            if old.strip().casefold() != new.strip().casefold():
                break  # New punctuation must agree as well.
            agreed += 1
        close_tail = quiet and agreed == len(tail)
        agreed = min(agreed, len(tail) if close_tail else max(0, len(tail) - self.hold_words))
        if (not self.confirmed and agreed == 1
                and tail[0].rstrip().endswith(('...', '\u2026'))):
            # A lone opening fragment explicitly marked unfinished must remain
            # a draft until a longer phrase agrees (or the caller flushes EOS).
            self._first_short_word = None
            agreed = 0
        elif not self.confirmed and agreed == 1 and len(keys(tail[:1])[0]) < 4:
            # An isolated short leading token can be an unfinished syllable.
            # Keep it visible as draft for one extra growing observation, rather
            # than closing a stub that the next hypothesis cannot reconcile.
            key = tail[0].strip().casefold()
            if self._first_short_word is None or self._first_short_word[0] != key:
                self._first_short_word = (key,audio_end)
                agreed = 0
            elif audio_end < self._first_short_word[1]+self.min_new_audio_s:
                agreed = 0
        else:
            self._first_short_word = None
        committed, remaining = tail[:agreed], tail[agreed:]
        self.confirmed.extend(committed)
        self.previous, self.previous_end = remaining, audio_end
        self.draft = "".join(remaining).strip()
        if committed:
            self._first_short_word = None
            self.sequence += 1
            self.history.append({"sequence": self.sequence, "text": "".join(committed).strip()})
            self.history = self.history[-10:]
        return committed

    def move_window(self, cut):
        # Only confirmed audio was removed. The unconfirmed suffix and its
        # previous audio endpoint still describe retained audio, so keep the
        # agreement evidence. Clearing it can make the very next (already long)
        # cropped window impossible to confirm/trim before the safety limit.
        self._short_recovery = None
        self._rewrite_recovery = None
        self._acoustic_recovery = None
        # Alignment positions are absolute. Evidence for retained confirmed
        # audio remains valid after cropping, just like previous text agreement.
        # Requiring a new pair here can make an already-long next window fail
        # before it has a chance to advance. Still require genuinely growing
        # audio and check the boundary again in the newly cropped alignment.
        if self._trim_evidence is not None:
            _, end, candidates = self._trim_evidence
            candidates = [c for c in candidates if c[0] > cut + .001]
            self._trim_evidence = (cut, end, candidates) if candidates else None
        self._first_short_word = None
        self.timed = [w for w in self.timed if w.end > cut + .001]
        self.confirmed = [w.text for w in self.timed]

    def close_utterance(self, items):
        """Finalize a completed, VAD-bounded utterance, explicitly labelled.

        The caller verifies the acoustic endpoint and locates existing closed
        context first. This is a best final utterance hypothesis, not a claim
        that its unstable tail passed ordinary prefix agreement.
        """
        count = self.prefix_length(items)
        if count is None:
            raise PrefixContextLost('pause recovery cannot locate closed context')
        new = items[count:]
        if new:
            self.sequence += 1
            self.history.append({'sequence': self.sequence, 'text': ''.join(new).strip(),
                                 'finalization_reason': 'vad_pause_recovery'})
            self.history = self.history[-10:]
        self.confirmed = list(items)
        self.previous, self.previous_end, self.draft = [], None, ''
        return new

    def packet(self):
        self.update_sequence += 1
        return {"version": 1, "sequence": self.update_sequence,
                "confirmed": list(self.history), "after_confirmed": self.sequence,
                "draft": self.draft}
