"""Bounded translation inputs for the negotiated immutable text-prefix protocol.

This groups text only. It does not confirm ASR, invent punctuation or acoustic
timestamps, or buffer original-language captions.
"""
import asyncio
from dataclasses import dataclass, replace
import math
import re
from typing import Awaitable, Callable

from asr.whisper_client import TranscriptionChunk


@dataclass
class TranslationInput:
    chunk: TranscriptionChunk
    grouping: dict | None = None


def prefix_stream_id(chunk):
    return chunk.chunk_id.split(":prefix:", 1)[0]


async def sentence_translation_inputs(inputs, *, max_words=80, max_chars=1200):
    """Retranslate a bounded growing sentence, never splice target fragments.

    New source IDs remain in source_chunk_ids once; context_source_chunk_ids
    records reused context. Closing an already translated tail needs no model
    call and carries no new source IDs. The caller caches its latest draft.
    """
    if max_words <= 0 or max_chars <= 0:
        raise ValueError('Sentence context limits must be positive')
    pending = []

    def packet(reason, *, close_only=False, closed=False):
        first, last = pending[0], pending[-1]
        ids = [key for item in pending for key in item.grouping['source_chunk_ids']]
        metadata = dict(last.grouping,
            sentence_key='mt-sentence:'+ids[0],
            sentence_closed=closed, sentence_close_reason=reason if closed else '',
            context_source_chunk_ids=ids, close_only=close_only)
        if close_only:
            metadata.update(reason=reason, source_chunk_ids=[], source_buffer_wait_ms=[], buffer_wait_ms=0.)
        chunk = replace(last.chunk, text=' '.join(item.chunk.text.strip() for item in pending))
        return TranslationInput(chunk, metadata)

    async for item in inputs:
        if item.grouping is None:
            if pending:
                yield packet('protocol_change', close_only=True, closed=True)
                pending = []
            yield item
            continue
        if pending:
            previous = pending[-1].chunk
            text = ' '.join([*(part.chunk.text.strip() for part in pending), item.chunk.text.strip()])
            old_stream = pending[-1].grouping['source_chunk_ids'][0].split(':prefix:', 1)[0]
            new_stream = item.grouping['source_chunk_ids'][0].split(':prefix:', 1)[0]
            changed = previous.language != item.chunk.language or old_stream != new_stream
            if changed or len(text.split()) > max_words or len(text) > max_chars:
                yield packet('source_change' if changed else 'context_limit', close_only=True, closed=True)
                pending = []
        pending.append(item)
        reason = item.grouping['reason']
        text = ' '.join(part.chunk.text.strip() for part in pending)
        closed = reason in {'sentence', 'eos', 'stream_end', 'language_change', 'protocol_change', 'source_change'}
        if not closed and (len(text.split()) >= max_words or len(text) >= max_chars):
            closed, reason = True, 'context_limit'
        yield packet(reason, closed=closed)
        if closed:
            pending = []
    if pending:
        yield packet('stream_end', close_only=True, closed=True)


class PrefixTranslationBuffer:
    def __init__(self, max_wait_s: float = 2.0, max_words: int = 40):
        if not math.isfinite(max_wait_s) or max_wait_s <= 0:
            raise ValueError("Translation grouping wait must be finite and positive")
        if max_words <= 0:
            raise ValueError("Translation grouping word limit must be positive")
        self.max_wait_s = max_wait_s
        self.max_words = max_words
        self.pending: list[tuple[TranscriptionChunk, float]] = []

    @property
    def deadline(self):
        return self.pending[0][1] + self.max_wait_s if self.pending else None

    def add(self, chunk, now):
        self.pending.append((chunk, now))
        text = " ".join(value.text.strip() for value, _ in self.pending)
        # Inspect existing punctuation only, including closing quotes/brackets.
        tail = text.rstrip().rstrip('\"\'”’»)]}')
        words = text.split()
        if tail.endswith(("!", "?", "。", "！", "？")):
            return "sentence"
        if tail.endswith("."):
            # Common initials/abbreviations are weak sentence boundaries.
            last = tail.split()[-1]
            if not re.fullmatch(r"(?:[^\W\d_]\.)+", last, re.UNICODE) and last.casefold() not in {
                "dr.", "dra.", "sr.", "sra.", "prof.", "núm.", "pàg.", "etc.",
            }:
                return "sentence"
        if tail.endswith((",", ";", ":")) and len(words) >= 6:
            return "clause"
        if len(words) >= self.max_words:
            return "word_limit"
        if chunk.finalization_reason == "eos":
            return "eos"
        return None

    def flush(self, reason, now):
        if not self.pending:
            return None
        pending, self.pending = self.pending, []
        first, last = pending[0][0], pending[-1][0]
        waits = [max(0.0, (now - received) * 1000) for _, received in pending]
        chunk = replace(last, text=" ".join(value.text.strip() for value, _ in pending),
                        chunk_id=f"mt-group:{first.chunk_id}:{last.chunk_id}", latency_ms=None)
        return TranslationInput(chunk, {
            "policy": "bsc_prefix_grouping_v1",
            "reason": reason,
            "source_chunk_ids": [value.chunk_id for value, _ in pending],
            "buffer_wait_ms": waits[0],
            "source_buffer_wait_ms": waits,
            "max_wait_ms": self.max_wait_s * 1000,
        })


async def group_prefix_translations(chunks, *, max_wait_s: float,
                                    on_prefix: Callable[[TranscriptionChunk], Awaitable[None]]):
    """Group prefix finals; pass conventional segments through unchanged.

    The deadline starts with the FIRST final, never slides with new arrivals,
    and fires even during silence or draft-only traffic. The next read survives
    a timeout, avoiding cancellation of the underlying ASR/queue iterator.
    """
    buffer = PrefixTranslationBuffer(max_wait_s)
    iterator = aiter(chunks)
    next_chunk = None
    loop = asyncio.get_running_loop()
    try:
        while True:
            if next_chunk is None:
                next_chunk = asyncio.create_task(anext(iterator))
            # Check age before consuming another ready item: continuous traffic
            # must not starve the deadline.
            deadline = buffer.deadline
            if deadline is not None and loop.time() >= deadline:
                yield buffer.flush("timeout", loop.time())
                continue
            timeout = None if deadline is None else max(0.0, deadline - loop.time())
            done, _ = await asyncio.wait({next_chunk}, timeout=timeout)
            if not done:
                yield buffer.flush("timeout", loop.time())
                continue
            try:
                chunk = next_chunk.result()
            except StopAsyncIteration:
                remaining = buffer.flush("stream_end", loop.time())
                if remaining:
                    yield remaining
                break
            finally:
                next_chunk = None

            if not chunk.prefix_protocol:
                remaining = buffer.flush("protocol_change", loop.time())
                if remaining:
                    yield remaining
                yield TranslationInput(chunk)
                continue
            # Publish source/draft immediately, before waiting for translation.
            await on_prefix(chunk)
            if chunk.is_partial or not chunk.text.strip():
                continue
            if buffer.pending and buffer.pending[-1][0].language != chunk.language:
                yield buffer.flush("language_change", loop.time())
            elif buffer.pending and prefix_stream_id(buffer.pending[-1][0]) != prefix_stream_id(chunk):
                yield buffer.flush("source_change", loop.time())
            reason = buffer.add(chunk, loop.time())
            if reason:
                yield buffer.flush(reason, loop.time())
    finally:
        if next_chunk is not None:
            next_chunk.cancel()
            await asyncio.gather(next_chunk, return_exceptions=True)
        if hasattr(iterator, "aclose"):
            await iterator.aclose()
