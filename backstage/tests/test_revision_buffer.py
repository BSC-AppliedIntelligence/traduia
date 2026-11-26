import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asr.whisper_client import TranscriptionChunk
from main import WhisperAgent


class FakeWhisper:
    def __init__(self, chunks):
        self._chunks = chunks

    async def receive_transcriptions(self):
        for delay, chunk in self._chunks:
            if delay:
                await asyncio.sleep(delay)
            yield chunk


def _chunk(text, reason, chunk_id, start=0.0, end=1.0, partial=False):
    return TranscriptionChunk(
        text=text,
        language="ca",
        is_partial=partial,
        start_time=start,
        end_time=end,
        finalization_reason=reason,
        chunk_id=chunk_id,
    )


def _agent_with_chunks(chunks, buffer_s=0.05):
    agent = WhisperAgent(
        target_language="en",
        event_id="event",
        audio_publisher=None,
        room=None,
        translator=None,
    )
    agent.whisper = FakeWhisper(chunks)
    agent._timeout_revision_buffer_s = buffer_s
    return agent


def _run(coro):
    return asyncio.run(coro)


def test_overlapping_stable_supersedes_buffered_timeout():
    async def scenario():
        timeout = _chunk("primera hipotesi", "timeout", "timeout-1", start=10.0, end=12.0)
        stable = _chunk("primera hipotesi corregida", "stable", "stable-1", start=10.0, end=12.2)
        agent = _agent_with_chunks([(0.0, timeout), (0.01, stable)])

        chunks = [chunk async for chunk in agent._revision_buffered_transcriptions()]

        assert chunks == [stable]

    _run(scenario())


def test_buffered_timeout_is_released_without_superseding_final():
    async def scenario():
        timeout = _chunk("primera hipotesi", "timeout", "timeout-1", start=10.0, end=12.0)
        later = _chunk("frase nova", "stable", "stable-2", start=20.0, end=21.0)
        agent = _agent_with_chunks([(0.0, timeout), (0.08, later)], buffer_s=0.03)

        chunks = [chunk async for chunk in agent._revision_buffered_transcriptions()]

        assert chunks == [timeout, later]

    _run(scenario())


def test_late_overlapping_stable_revision_is_detected():
    timeout = _chunk("Actualment és...", "timeout", "timeout-1", start=24.5, end=26.0)
    stable = _chunk(
        "actualment és Henry Lee Professor of Economics",
        "stable",
        "stable-1",
        start=24.4,
        end=29.6,
    )
    agent = _agent_with_chunks([])

    match = agent._find_late_revision(
        [{"chunk": timeout, "released_at": 10.0}],
        stable,
        now=13.25,
    )

    assert match is not None
    previous, delta_ms, overlap, similarity = match
    assert previous is timeout
    assert delta_ms == 3250.0
    assert overlap >= 0.95
    assert similarity >= 0.35


def test_non_overlapping_stable_revision_is_ignored():
    timeout = _chunk("primera hipotesi", "timeout", "timeout-1", start=10.0, end=12.0)
    stable = _chunk("frase nova", "stable", "stable-1", start=20.0, end=21.0)
    agent = _agent_with_chunks([])

    match = agent._find_late_revision(
        [{"chunk": timeout, "released_at": 10.0}],
        stable,
        now=12.0,
    )

    assert match is None


if __name__ == "__main__":
    test_overlapping_stable_supersedes_buffered_timeout()
    test_buffered_timeout_is_released_without_superseding_final()
    test_late_overlapping_stable_revision_is_detected()
    test_non_overlapping_stable_revision_is_ignored()
