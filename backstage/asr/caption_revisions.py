"""Reconcile source text before captions and shared translation fan-out.

A bounded ASR window retains context which can be transcribed again. Preserve
its unique prefix when a later result corrects that tail, and remove exact
context already delivered in an earlier, closed source span. Text similarity
alone never authorizes dropping a result.
"""
from collections import deque
from dataclasses import replace
from difflib import SequenceMatcher
import re

from asr.whisper_client import TranscriptionChunk


def _words(text):
    return list(re.finditer(r"\w+", text, re.UNICODE))


def _key(text):
    return [m.group().casefold() for m in _words(text)]


def _overlap(a, b):
    duration = min(a.end_time - a.start_time, b.end_time - b.start_time)
    if duration <= 0:
        return 0
    return max(0, min(a.end_time, b.end_time) - max(a.start_time, b.start_time)) / duration


def _replace_name_tail(previous, current):
    """Recognize a repeated name or a distinctive name with a truncated surname.

    This narrow fallback is used only with shared audio. A distinctive matching
    name pair or a short capitalized trailing fragment can identify a correction
    even when too few words exist for the general tail alignment.
    """
    old_words, new_words = _words(previous), _words(current)
    old, new = _key(previous), _key(current)
    articles = {'la', 'l', 'el', 'en', 'na', 'the'}
    if len(old) < 2 or len(new) < 2:
        return None
    i = len(old) - 2
    name, fragment = old_words[i].group(), old_words[-1].group()
    if not (name[0].isupper() and len(name) >= 4 and fragment[0].isupper()):
        return None
    if old.count(old[i]) != 1:
        return None
    for j in range(min(2, len(new)-1)):
        replacement = new_words[j+1].group()
        same_name = new[j+1] == old[-1]
        expanded_fragment = len(fragment) <= 4 and len(replacement) >= max(4, len(fragment)+2)
        if (new[j] != old[i] or not new_words[j].group()[0].isupper()
                or not replacement[0].isupper() or not (same_name or expanded_fragment)):
            continue
        if j and (i < j or old[i-1] not in articles or new[0] not in articles):
            continue
        prefix = previous[:old_words[i-j].start()].rstrip()
        return (prefix + ' ' + current).strip()
    return None


def _replace_short_tail(previous, current):
    """Align a short suffix to the new prefix, only for overlapping audio.

    Unlike the name fallback, this works without capitalization: a distinctive
    repeated phrase, completed word, or closely spelled corrected last word
    can locate the splice. Do not align arbitrary replacements from one common
    word. A repeated anchor inside the old text is ambiguous, so retain it.
    """
    old_words, new_words = _words(previous), _words(current)
    old, new = _key(previous), _key(current)
    for count in range(min(len(old), len(new)), 0, -1):
        suffix, prefix = old[-count:], new[:count]
        if suffix[:-1] != prefix[:-1]:
            continue
        before, after = suffix[-1], prefix[-1]
        exact = before == after
        shared_prefix = 0
        for a, b in zip(before, after):
            if a != b:
                break
            shared_prefix += 1
        completion = (len(before) >= 3 and after.startswith(before)
                      and len(after) >= len(before) + 2)
        spelling_correction = (min(len(before), len(after)) >= 4
                               and shared_prefix >= 3
                               and SequenceMatcher(None, before, after, autojunk=False).ratio() >= 0.7)
        if exact:
            # One complete word, or a short function-word phrase, is too weak.
            if count < 2 or len(' '.join(suffix)) < 10 or max(map(len, suffix)) < 5:
                continue
        elif not (completion or spelling_correction):
            continue
        elif count == 1:
            # With no complete word beside it, require a longer literal
            # incomplete-word anchor; a merely similar word is insufficient.
            if not completion or len(before) < 5:
                continue
        elif len(' '.join(suffix[:-1])) + shared_prefix < 7:
            continue
        occurrences = sum(
            old[i:i+count-1] == suffix[:-1]
            and (old[i+count-1] == before
                 or (completion and old[i+count-1].startswith(before)))
            for i in range(len(old)-count+1))
        if occurrences != 1:
            continue
        start = old_words[len(old)-count].start()
        retained = previous[:start].rstrip()
        separator = '' if retained.endswith(("'", '’', '-')) else ' '
        return (retained + separator + current).strip()
    # Preserve the earlier narrow rule for short misrecognized surnames which
    # share no spelling with the corrected surname (e.g. Om -> Armstrong).
    return _replace_name_tail(previous, current)


def _replace_tail(previous, current):
    """Align current to a suffix, keeping the untouched prefix verbatim.

    Leading previous words have zero cost. Diagonal edit steps win ties, so a
    changed word ('tan' -> 'en') is replaced rather than retained before the
    correction. Require several shared words; ambiguous overlap stays visible.
    """
    old, new = _key(previous), _key(current)
    if not old or not new:
        return None
    costs = [[0] * (len(new) + 1) for _ in range(len(old) + 1)]
    for j in range(1, len(new) + 1):
        costs[0][j] = j
    for i in range(1, len(old) + 1):
        for j in range(1, len(new) + 1):
            costs[i][j] = min(costs[i-1][j-1] + (old[i-1] != new[j-1]),
                              costs[i-1][j] + 1, costs[i][j-1] + 1)
    i, j, matches = len(old), len(new), 0
    while j:
        if i and costs[i][j] == costs[i-1][j-1] + (old[i-1] != new[j-1]):
            matches += old[i-1] == new[j-1]
            i, j = i-1, j-1
        elif i and costs[i][j] == costs[i-1][j] + 1:
            i -= 1
        else:
            j -= 1
    if matches < 3 or matches < min(len(old) - i, len(new)) * 0.6:
        return _replace_short_tail(previous, current)
    prefix = previous[:_words(previous)[i].start()].rstrip() if i < len(old) else previous
    return (prefix + ' ' + current).strip()


class SourceCaptionRevisions:
    def __init__(self):
        self.draft: TranscriptionChunk | None = None
        self.closed = deque(maxlen=20)
        self._retained_context = False

    def _trim_closed_prefix(self, chunk):
        for previous in self.closed:
            if _overlap(previous, chunk) <= 0:
                continue
            old, new = _key(previous.text), _key(chunk.text)
            for count in range(min(len(old), len(new)), 0, -1):
                if old[-count:] != new[:count]:
                    continue
                # A whole repeated span is safe to discard. For cross-boundary
                # context require a multiword anchor, not a common 'i'/'en'.
                if count < len(new) and (count < 3 or len(' '.join(new[:count])) < 16):
                    continue
                if count == len(new):
                    return None
                if chunk.end_time <= previous.end_time:
                    continue
                chunk = replace(chunk, text=chunk.text[_words(chunk.text)[count].start():],
                                start_time=max(chunk.start_time, previous.end_time))
                break
        return chunk

    def receive(self, chunk: TranscriptionChunk) -> TranscriptionChunk | None:
        if chunk.prefix_protocol:
            # The source already supplies closed prefix IDs. Its text has no
            # acoustic span, so time-overlap revision heuristics do not apply.
            return chunk
        chunk = self._trim_closed_prefix(chunk)
        if chunk is None:
            return None
        previous = self.draft
        same_draft = previous is not None and _overlap(previous, chunk) >= 0.65
        aligned_tail = None
        if (not same_draft and previous is not None
                and min(previous.end_time, chunk.end_time) - max(previous.start_time, chunk.start_time) >= 0.25
                and chunk.start_time > previous.start_time + 0.75
                and chunk.end_time >= previous.end_time - 0.25):
            # A tail can extend beyond the window, making its overlap fraction
            # small. Actual shared audio plus a word alignment still identifies
            # that continuation without merging unrelated adjacent chunks.
            aligned_tail = (_replace_tail(previous.text, chunk.text) if self._retained_context
                            else _replace_short_tail(previous.text, chunk.text))
            same_draft = aligned_tail is not None
        if same_draft:
            old, new = _key(previous.text), _key(chunk.text)
            if (self._retained_context
                    and chunk.start_time > previous.start_time + 0.75
                    and chunk.end_time < previous.end_time - 0.25
                    and len(new) >= 3 and len(' '.join(new)) >= 16
                    and any(old[i:i+len(new)] == new for i in range(len(old)-len(new)+1))):
                # A retained window can be re-segmented into several stable
                # interior pieces. These words are already in the live draft;
                # do not append them or close its still-unconfirmed tail.
                return None
            # Whisper can shift an utterance's leading timestamp slightly
            # while expanding/correcting a short timeout hypothesis.
            if chunk.start_time > previous.start_time + 0.75:
                if chunk.end_time < previous.end_time - 0.25:
                    merged = None
                else:
                    merged = aligned_tail if aligned_tail is not None else _replace_tail(previous.text, chunk.text)
                if merged is not None:
                    chunk = replace(chunk, text=merged, start_time=previous.start_time,
                                    end_time=max(previous.end_time, chunk.end_time))
                else:
                    # Cannot safely locate the correction. Preserve both texts
                    # instead of guessing a splice which could remove words.
                    chunk = replace(chunk, text=previous.text + '\n' + chunk.text,
                                    start_time=min(previous.start_time, chunk.start_time),
                                    end_time=max(previous.end_time, chunk.end_time))
            if previous is not None and _key(chunk.text) == _key(previous.text):
                if chunk.is_partial or chunk.finalization_reason in {'timeout', 'bounded_window'}:
                    return None
        if chunk.is_partial:
            return chunk
        self._retained_context = (same_draft and self._retained_context) or chunk.finalization_reason == 'bounded_window'
        if self.draft is not None and (previous is None or _overlap(self.draft, chunk) < 0.65):
            self.closed.append(self.draft)
        if chunk.finalization_reason in {'timeout', 'bounded_window'}:
            self.draft = chunk
        else:
            self.closed.append(chunk)
            self.draft = None
            self._retained_context = False
        return chunk
