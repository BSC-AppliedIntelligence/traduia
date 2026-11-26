import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asr.whisper_client import WhisperLiveClient


def _message(text, start=0.0, end=1.0):
    return {"segments": [{"text": text, "start": str(start), "end": str(end)}]}


def _run(coro):
    return asyncio.run(coro)


async def _close_generator(gen):
    try:
        await gen.aclose()
    except RuntimeError:
        pass


def test_repeated_identical_partial_does_not_reset_active_timeout():
    async def scenario():
        client = WhisperLiveClient("127.0.0.1", 9090, active_timeout_s=0.05)
        gen = client.receive_transcriptions()

        client._receive_queue.put_nowait(_message("hola"))
        first = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert first.is_partial is True
        assert first.text == "hola"

        started = time.perf_counter()
        await asyncio.sleep(0.03)
        client._receive_queue.put_nowait(_message("hola"))

        final = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        elapsed = time.perf_counter() - started

        assert final.is_partial is False
        assert final.text == "hola"
        assert final.finalization_reason == "timeout"
        assert elapsed < 0.09
        await _close_generator(gen)

    _run(scenario())


def test_changed_partial_resets_active_timeout_deadline():
    async def scenario():
        timeout_s = 0.05
        client = WhisperLiveClient("127.0.0.1", 9090, active_timeout_s=timeout_s)
        gen = client.receive_transcriptions()

        client._receive_queue.put_nowait(_message("hola", end=1.0))
        first = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert first.is_partial is True

        await asyncio.sleep(0.03)
        changed_at = time.perf_counter()
        client._receive_queue.put_nowait(_message("hola mon", end=1.4))
        second = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert second.is_partial is True
        assert second.text == "hola mon"

        final = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        elapsed_after_change = time.perf_counter() - changed_at

        assert final.is_partial is False
        assert final.text == "hola mon"
        assert final.finalization_reason == "timeout"
        assert elapsed_after_change >= timeout_s * 0.8
        await _close_generator(gen)

    _run(scenario())


def test_completed_segment_is_final_with_server_reason():
    async def scenario():
        client = WhisperLiveClient("127.0.0.1", 9090, active_timeout_s=10.0)
        gen = client.receive_transcriptions()

        client._receive_queue.put_nowait({
            "segments": [{
                "text": "hola mon",
                "start": "0.000",
                "end": "8.000",
                "completed": True,
                "finalization_reason": "bounded_window",
            }]
        })

        final = await asyncio.wait_for(gen.__anext__(), timeout=0.2)

        assert final.is_partial is False
        assert final.text == "hola mon"
        assert final.start_time == 0.0
        assert final.end_time == 8.0
        assert final.finalization_reason == "bounded_window"
        await _close_generator(gen)

    _run(scenario())


def test_overlapping_stable_confirmation_after_timeout_is_suppressed():
    async def scenario():
        client = WhisperLiveClient("127.0.0.1", 9090, active_timeout_s=0.05)
        gen = client.receive_transcriptions()

        client._receive_queue.put_nowait(_message("hola mon", start=7.761, end=10.200))
        partial = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert partial.is_partial is True

        final = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert final.is_partial is False
        assert final.finalization_reason == "timeout"
        assert final.text == "hola mon"

        client._receive_queue.put_nowait({
            "segments": [
                {"text": "hola mon", "start": "7.761", "end": "9.761"},
                {"text": "seguent frase", "start": "9.761", "end": "11.010"},
            ]
        })

        next_chunk = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert next_chunk.is_partial is True
        assert next_chunk.text == "seguent frase"
        await _close_generator(gen)

    _run(scenario())


def test_same_text_in_non_overlapping_audio_is_not_suppressed():
    async def scenario():
        client = WhisperLiveClient("127.0.0.1", 9090, active_timeout_s=10.0)
        gen = client.receive_transcriptions()

        client._receive_queue.put_nowait({
            "segments": [
                {"text": "hola", "start": "0.000", "end": "1.000", "completed": True},
            ]
        })
        first = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert first.is_partial is False
        assert first.text == "hola"

        client._receive_queue.put_nowait({
            "segments": [
                {"text": "hola", "start": "3.000", "end": "4.000", "completed": True},
            ]
        })
        second = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert second.is_partial is False
        assert second.text == "hola"
        assert second.start_time == 3.0
        assert second.end_time == 4.0
        await _close_generator(gen)

    _run(scenario())


def test_punctuation_only_revision_is_suppressed():
    async def scenario():
        client = WhisperLiveClient("127.0.0.1", 9090, active_timeout_s=0.05)
        gen = client.receive_transcriptions()

        client._receive_queue.put_nowait(_message("mercat de treball.", start=10.0, end=12.0))
        partial = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert partial.is_partial is True

        final = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert final.is_partial is False
        assert final.text == "mercat de treball."

        client._receive_queue.put_nowait({
            "segments": [
                {"text": "mercat de treball", "start": "10.000", "end": "12.000"},
                {"text": "seguent", "start": "12.000", "end": "13.000"},
            ]
        })
        next_chunk = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert next_chunk.is_partial is True
        assert next_chunk.text == "seguent"
        await _close_generator(gen)

    _run(scenario())


def test_word_correction_in_overlapping_revision_is_forwarded():
    async def scenario():
        client = WhisperLiveClient("127.0.0.1", 9090, active_timeout_s=0.05)
        gen = client.receive_transcriptions()

        client._receive_queue.put_nowait(_message("per analitzar la pobresa o per lluitar contra la pobresa a nivell global?", start=103.677, end=108.677))
        partial = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert partial.is_partial is True

        final = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert final.is_partial is False

        client._receive_queue.put_nowait({
            "segments": [
                {"text": "per analitzar la pobresa o per lluitar contra la probresa a nivell global.", "start": "103.677", "end": "108.677"},
                {"text": "seguent frase", "start": "109.000", "end": "110.000"},
            ]
        })
        next_chunk = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert next_chunk.is_partial is False
        assert next_chunk.text == "per analitzar la pobresa o per lluitar contra la probresa a nivell global."
        next_partial = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert next_partial.text == "seguent frase"
        await _close_generator(gen)

    _run(scenario())


def test_bounded_window_extension_of_timeout_preserves_new_words():
    async def scenario():
        client = WhisperLiveClient("127.0.0.1", 9090, active_timeout_s=0.05)
        gen = client.receive_transcriptions()

        client._receive_queue.put_nowait(_message("Com reconeix l'academia sueca quan fa el discurs...", start=125.625, end=131.625))
        partial = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert partial.is_partial is True

        final = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert final.is_partial is False

        client._receive_queue.put_nowait({
            "segments": [{
                "text": "Com reconeix l'academia sueca quan fa el discurs per otorgar el premi i tots els escrits",
                "start": "125.625",
                "end": "135.625",
                "completed": True,
                "finalization_reason": "bounded_window",
            }]
        })
        client._receive_queue.put_nowait(_message("seguent idea", start=135.625, end=136.625))
        next_chunk = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert next_chunk.is_partial is False
        assert next_chunk.finalization_reason == "bounded_window"
        assert next_chunk.text.endswith("per otorgar el premi i tots els escrits")
        next_partial = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert next_partial.text == "seguent idea"
        await _close_generator(gen)

    _run(scenario())


@pytest.mark.parametrize("reason", ["stable", "bounded_window", "eos"])
def test_claudia_sentence_extension_survives_timeout_and_duplicate_confirmation(reason):
    async def scenario():
        client = WhisperLiveClient("127.0.0.1", 9090, active_timeout_s=0.01)
        gen = client.receive_transcriptions()
        prefix = "Aquí ja començo destacant..."
        full = "Aquí ja començo destacant que la Clàudia Goldin va ser la primera dona"
        client._receive_queue.put_nowait(_message(prefix, start=42.699, end=44.621))
        assert (await anext(gen)).is_partial
        assert (await asyncio.wait_for(anext(gen), timeout=0.2)).text == prefix

        message = {"segments": [{"text": full, "start": 42.699, "end": 47.189,
                                  "completed": True, "finalization_reason": reason}]}
        client._receive_queue.put_nowait(message)
        client._receive_queue.put_nowait(message)
        client._receive_queue.put_nowait(None)
        remaining = [chunk async for chunk in gen]
        assert [(c.text, c.finalization_reason) for c in remaining] == [(full, reason)]
    _run(scenario())


@pytest.mark.parametrize("finish", ["timeout", "eos"])
def test_expanded_active_hypothesis_is_not_lost_before_timeout_or_stream_end(finish):
    async def scenario():
        client = WhisperLiveClient("127.0.0.1", 9090, active_timeout_s=0.01)
        gen = client.receive_transcriptions()
        prefix = "Aquí ja començo destacant..."
        full = "Aquí ja començo destacant que la Clàudia Goldin va ser la primera dona"
        client._receive_queue.put_nowait(_message(prefix, start=42.699, end=44.621))
        assert (await anext(gen)).is_partial
        assert (await asyncio.wait_for(anext(gen), timeout=0.2)).text == prefix
        client._receive_queue.put_nowait(_message(full, start=42.699, end=47.189))
        if finish == "eos":
            client._receive_queue.put_nowait(None)
        partial = await asyncio.wait_for(anext(gen), timeout=0.2)
        assert partial.is_partial and partial.text == full
        final = await asyncio.wait_for(anext(gen), timeout=0.2)
        assert final.text == full and final.finalization_reason == finish
        await _close_generator(gen)
    _run(scenario())


def test_duplicate_filter_does_not_suppress_a_short_meaningful_correction():
    old = "La investigadora pot publicar el treball."
    correction = "La investigadora no pot publicar el treball."
    assert not WhisperLiveClient._final_already_emitted(
        {WhisperLiveClient._final_key(old): [(10.0, 13.0)]},
        [(old, 10.0, 13.0)], correction, 10.0, 13.0,
    )


def test_invalid_timestamp_segment_is_skipped():
    async def scenario():
        client = WhisperLiveClient("127.0.0.1", 9090, active_timeout_s=10.0)
        gen = client.receive_transcriptions()

        client._receive_queue.put_nowait({
            "segments": [
                {"text": "temps dolent", "start": "65.111", "end": "41.135"},
                {"text": "segment valid", "start": "41.135", "end": "43.135"},
            ]
        })
        chunk = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
        assert chunk.is_partial is True
        assert chunk.text == "segment valid"
        await _close_generator(gen)

    _run(scenario())


def test_close_sends_end_of_audio_bytes():
    class DummyWebSocket:
        def __init__(self):
            self.sent = []
            self.closed = False

        async def send(self, message):
            self.sent.append(message)

        async def close(self):
            self.closed = True

    async def scenario():
        client = WhisperLiveClient("127.0.0.1", 9090)
        ws = DummyWebSocket()
        client._ws = ws
        client._connected = True

        await client.close()

        assert ws.sent == [b"END_OF_AUDIO"]
        assert ws.closed is True

    _run(scenario())


if __name__ == "__main__":
    test_repeated_identical_partial_does_not_reset_active_timeout()
    test_changed_partial_resets_active_timeout_deadline()
    test_completed_segment_is_final_with_server_reason()
    test_overlapping_stable_confirmation_after_timeout_is_suppressed()
    test_same_text_in_non_overlapping_audio_is_not_suppressed()
    test_punctuation_only_revision_is_suppressed()
    test_word_correction_in_overlapping_revision_is_forwarded()
    test_bounded_window_extension_of_timeout_preserves_new_words()
    test_invalid_timestamp_segment_is_skipped()
    test_close_sends_end_of_audio_bytes()
