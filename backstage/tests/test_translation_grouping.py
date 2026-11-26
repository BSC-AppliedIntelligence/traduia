"""Exercise the async deadline, exact source coverage and real agent fanout."""
import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from asr.whisper_client import TranscriptionChunk
from main import WhisperAgent
from translation.base import TranslationResult
from translation.grouping import PrefixTranslationBuffer, group_prefix_translations


def prefix(text, sequence=1, *, partial=False, reason="stable_prefix"):
    return TranscriptionChunk(text, language="ca", prefix_protocol=True,
                              is_partial=partial, finalization_reason=reason,
                              chunk_id=f"stream:prefix:{sequence}")


def test_buffer_preserves_words_punctuation_ids_and_first_deadline():
    buffer = PrefixTranslationBuffer(1.5)
    assert buffer.add(prefix("Les seves"), 10) is None
    assert buffer.add(prefix("primeres obres", 2), 11) is None
    assert buffer.deadline == 11.5
    assert buffer.add(prefix("són aquí.", 3), 11.2) == "sentence"
    result = buffer.flush("sentence", 11.2)
    assert result.chunk.text == "Les seves primeres obres són aquí."
    assert result.chunk.prefix_protocol and result.chunk.latency_ms is None
    assert result.grouping["source_chunk_ids"] == [f"stream:prefix:{i}" for i in [1, 2, 3]]
    assert result.grouping["source_buffer_wait_ms"] == pytest.approx([1200, 200, 0])
    assert buffer.deadline is None
    assert buffer.flush("stream_end", 12) is None


@pytest.mark.parametrize("text, reason", [
    ('Això és una frase."', "sentence"),
    ("La persona que parla avui aquí,", "clause"),
    ("La persona que parla avui aquí;", "clause"),
    ("Avui,", None), ("el Dr.", None), ("la inicial J.", None),
])
def test_boundaries_use_only_supplied_punctuation(text, reason):
    assert PrefixTranslationBuffer().add(prefix(text), 0) == reason


def test_reviewed_default_budget_is_two_seconds():
    buffer = PrefixTranslationBuffer()
    buffer.add(prefix('primer fragment'), 10)
    assert buffer.deadline == 12


def test_word_limit_and_eos_flush_without_adding_punctuation():
    buffer = PrefixTranslationBuffer(max_words=4)
    assert buffer.add(prefix("un dos tres quatre"), 0) == "word_limit"
    assert buffer.flush("word_limit", 0).chunk.text == "un dos tres quatre"
    assert buffer.add(prefix("última cua", 2, reason="eos"), 1) == "eos"


@pytest.mark.parametrize("wait", [0, -1, float("nan"), float("inf")])
def test_invalid_deadlines_are_refused(wait):
    with pytest.raises(ValueError):
        PrefixTranslationBuffer(wait)


def test_timeout_during_silence_does_not_cancel_or_lose_the_next_input():
    async def scenario():
        queue = asyncio.Queue()
        finalized = asyncio.Event()

        async def source():
            try:
                while (item := await queue.get()) is not None:
                    yield item
            finally:
                finalized.set()

        seen = []

        async def publish(value):
            seen.append(value.text)

        stream = group_prefix_translations(source(), max_wait_s=.03, on_prefix=publish)
        await queue.put(prefix("primer fragment"))
        result = await asyncio.wait_for(anext(stream), 1)
        assert result.grouping["reason"] == "timeout"
        assert result.grouping["buffer_wait_ms"] >= 20
        assert not finalized.is_set()
        await queue.put(prefix("segon fragment.", 2))
        result = await asyncio.wait_for(anext(stream), 1)
        assert result.chunk.text == "segon fragment."
        await queue.put(None)
        assert [item async for item in stream] == []
        assert seen == ["primer fragment", "segon fragment."]
        assert finalized.is_set()
    asyncio.run(scenario())


def test_continuous_drafts_do_not_reset_the_first_final_deadline():
    async def scenario():
        stopped = asyncio.Event()

        async def source():
            try:
                yield prefix("text confirmat")
                for i in range(100):
                    await asyncio.sleep(.005)
                    yield prefix(f"esborrany {i}", i + 2, partial=True)
            finally:
                stopped.set()

        callback = AsyncMock()
        stream = group_prefix_translations(source(), max_wait_s=.035, on_prefix=callback)
        result = await asyncio.wait_for(anext(stream), .5)
        assert result.chunk.text == "text confirmat"
        assert result.grouping["reason"] == "timeout"
        assert result.grouping["buffer_wait_ms"] < 200
        assert callback.await_count > 1
        await stream.aclose()
        assert stopped.is_set()
    asyncio.run(scenario())


def test_protocol_and_language_changes_never_merge_distinct_sources():
    async def scenario():
        values = [prefix("català"), replace(prefix("English", 2), language="en"),
                  TranscriptionChunk("conventional", chunk_id="regular")]

        async def source():
            for value in values:
                yield value

        result = [item async for item in group_prefix_translations(
            source(), max_wait_s=1.5, on_prefix=AsyncMock())]
        assert [item.chunk.text for item in result] == ["català", "English", "conventional"]
        assert [item.grouping["reason"] for item in result[:2]] == ["language_change", "protocol_change"]
        assert result[-1].grouping is None and result[-1].chunk is values[-1]
    asyncio.run(scenario())


class Participant:
    def __init__(self):
        self.messages = []

    async def publish_data(self, payload, **kwargs):
        self.messages.append(json.loads(payload))


def agent(language="en", *, primary=False, owns=False):
    room = type("Room", (), {"local_participant": Participant()})()
    target = WhisperAgent(language, "group-test", None, room,
                          is_primary_agent=primary, owns_whisper=owns)
    target.translator = type("Translator", (), {
        "translate_with_status": AsyncMock(side_effect=lambda text, **kw: TranslationResult(f"{language}:{text}"))
    })()
    return target


@pytest.mark.parametrize("language", ["es", "en"])
def test_shared_fanout_preserves_immediate_ca_and_groups_each_target(language):
    async def scenario():
        owner = agent("original", primary=True, owns=True)
        target = agent(language)
        await owner.start()
        await target.start()
        await target.start_source_chunk_consumer()
        owner.fanout_agents = [target]

        async def source():
            yield prefix("Les seves")
            yield prefix("primeres obres", 2)
            yield prefix("són aquí.", 3)
            yield prefix("una prova", 4)
            yield prefix("una prova", 5)

        await owner._process_chunk_stream(source())
        # No sentence wait was inserted into the source owner.
        assert [m["text"] for m in owner.room.local_participant.messages] == [
            "Les seves", "primeres obres", "són aquí.", "una prova", "una prova"]
        await target.finish_source_chunks()
        captions = target.room.local_participant.messages
        assert [m["text"] for m in captions] == [
            f"{language}:Les seves primeres obres són aquí.", f"{language}:una prova una prova"]
        assert [m["translation_group"]["reason"] for m in captions] == ["sentence", "stream_end"]
        assert [len(m["translation_group"]["source_chunk_ids"]) for m in captions] == [3, 2]
        assert all(m["revision_closed"] and m["audio_start_s"] is None and m["audio_end_s"] is None
                   for m in captions)
        assert target.translator.translate_with_status.await_count == 2
        await owner.stop()
        await target.stop()
    asyncio.run(scenario())


def test_slow_translation_does_not_block_shared_source_captions():
    async def scenario():
        owner, target = agent("original", primary=True, owns=True), agent()
        await owner.start()
        await target.start()
        started, release = asyncio.Event(), asyncio.Event()

        async def translate(text, **kwargs):
            started.set()
            await release.wait()
            return TranslationResult(text)

        target.translator.translate_with_status.side_effect = translate
        await target.start_source_chunk_consumer()
        owner.fanout_agents = [target]

        async def source():
            yield prefix("Primera frase.")
            await asyncio.wait_for(started.wait(), 1)
            yield prefix("Segona frase.", 2)

        await asyncio.wait_for(owner._process_chunk_stream(source()), 1)
        assert len(owner.room.local_participant.messages) == 2
        assert target.room.local_participant.messages == []
        release.set()
        await target.finish_source_chunks()
        assert len(target.room.local_participant.messages) == 2
        await owner.stop()
        await target.stop()
    asyncio.run(scenario())


def test_legacy_primary_still_publishes_prefixes_before_grouped_translation():
    async def scenario():
        target = agent(primary=True, owns=True)
        await target.start()

        async def source():
            yield prefix("Una primera")
            # Pulling the second chunk must not require waiting for MT.
            assert [m["text"] for m in target.room.local_participant.messages] == ["Una primera"]
            yield prefix("frase.", 2)

        await target._process_chunk_stream(source())
        messages = target.room.local_participant.messages
        assert [m["channel"] for m in messages] == ["original", "original", "en"]
        assert messages[-1]["text"] == "en:Una primera frase."
        await target.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("language, grouping_enabled", [("en", False), ("fr", True)])
def test_disable_switch_and_other_targets_preserve_per_prefix_calls(language, grouping_enabled):
    async def scenario():
        target = agent(language)
        await target.start()
        if not grouping_enabled:
            target._translation_max_wait_s = 0

        async def source():
            yield prefix("una prova")
            yield prefix("una prova", 2)

        await target._process_chunk_stream(source())
        assert target.translator.translate_with_status.await_count == 2
        assert all("translation_group" not in m for m in target.room.local_participant.messages)
        await target.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("model", ["small", "Systran/faster-whisper-large-v3",
                                   "BSC-LT/faster-whisper-large-v3-ca-punctuated-3370h"])
def test_non_prefix_models_keep_existing_segment_translation(model):
    async def scenario():
        target = agent()
        target.whisper_model = model
        await target.start()

        async def source():
            for i, text in enumerate(["un fragment", "una continuació"]):
                yield TranscriptionChunk(text, language="ca", chunk_id=str(i), finalization_reason="stable")

        await target._process_chunk_stream(source())
        assert target.translator.translate_with_status.await_count == 2
        assert all("translation_group" not in m for m in target.room.local_participant.messages)
        await target.stop()
    asyncio.run(scenario())


def test_grouped_fallback_is_explicit_and_retains_source_coverage():
    async def scenario():
        target = agent()
        await target.start()
        target.translator.translate_with_status.side_effect = lambda text, **kw: TranslationResult(text, "fallback", "timeout")

        async def source():
            yield prefix("text")
            yield prefix("confirmat.", 2)

        with patch("main.captions_manager.broadcast", AsyncMock()) as broadcast, \
             patch.dict("main.event_id_to_code", {"group-test": "TEST"}):
            await target._process_chunk_stream(source())
        caption, = target.room.local_participant.messages
        assert caption["text"] == "text confirmat."
        assert caption["translation_fallback"] is True
        assert caption["translation_error"] == "timeout"
        assert caption["text_language"] == "ca"
        assert len(caption["translation_group"]["source_chunk_ids"]) == 2
        assert json.loads(broadcast.await_args.args[2]) == caption
        await target.stop()
    asyncio.run(scenario())


def test_stop_drains_buffered_and_queued_finals_before_deactivation():
    async def scenario():
        target = agent()
        await target.start()
        await target.start_source_chunk_consumer()
        await target.enqueue_source_chunk(prefix("la cua"))
        await target.enqueue_source_chunk(prefix("final", 2))
        await target.stop()
        caption, = target.room.local_participant.messages
        assert caption["text"] == "en:la cua final"
        assert caption["translation_group"]["reason"] == "stream_end"
        assert not target.is_running and target._source_chunk_task is None
    asyncio.run(scenario())
