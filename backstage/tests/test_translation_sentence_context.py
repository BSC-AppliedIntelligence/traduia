"""Growing MT context fixes fragment grammar without changing letter case by rule."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from asr.whisper_client import TranscriptionChunk
from translation.base import TranslationResult
from translation.grouping import TranslationInput, sentence_translation_inputs
from test_translation_grouping import agent, prefix


def grouped(text, sequence=1, reason='timeout', stream='source', language='ca'):
    chunk = replace(prefix(text, sequence), chunk_id=f'{stream}:prefix:{sequence}', language=language)
    return TranslationInput(chunk, {'reason': reason, 'source_chunk_ids': [chunk.chunk_id],
        'buffer_wait_ms': 2000., 'source_buffer_wait_ms': [2000.], 'max_wait_ms': 2000.})


async def values(items):
    for item in items:
        yield item


def test_context_accumulates_but_new_source_ids_are_not_duplicated():
    async def scenario():
        items = [grouped('I en tercer lloc, hi ha una'), grouped('altra qüestió', 2),
                 grouped('que és interessant.', 3, 'sentence'), grouped('Barcelona.', 4, 'sentence')]
        result = [x async for x in sentence_translation_inputs(values(items))]
        assert [x.chunk.text for x in result] == ['I en tercer lloc, hi ha una',
            'I en tercer lloc, hi ha una altra qüestió',
            'I en tercer lloc, hi ha una altra qüestió que és interessant.', 'Barcelona.']
        assert [x.grouping['sentence_closed'] for x in result] == [False, False, True, True]
        assert len({x.grouping['sentence_key'] for x in result[:3]}) == 1
        assert result[-1].grouping['sentence_key'] != result[0].grouping['sentence_key']
        assert [i for x in result for i in x.grouping['source_chunk_ids']] == [
            f'source:prefix:{i}' for i in range(1, 5)]
        assert result[2].grouping['context_source_chunk_ids'] == [f'source:prefix:{i}' for i in range(1,4)]
    asyncio.run(scenario())


@pytest.mark.parametrize('limit', [{'max_words': 3}, {'max_chars': 15}])
def test_context_limit_closes_previous_text_without_losing_the_next_input(limit):
    async def scenario():
        result = [x async for x in sentence_translation_inputs(values([
            grouped('primera part'), grouped('una altra part', 2)]), **limit)]
        expected = ['primera part', 'primera part', 'una altra part']
        if limit.get('max_chars'):
            expected.append('una altra part')
        assert [x.chunk.text for x in result] == expected
        assert result[1].grouping['close_only'] and result[1].grouping['sentence_closed']
        assert result[1].grouping['sentence_close_reason'] == 'context_limit'
        assert result[1].grouping['source_chunk_ids'] == []
        assert result[2].grouping['sentence_key'] != result[0].grouping['sentence_key']
    asyncio.run(scenario())


@pytest.mark.parametrize('next_item', [grouped('new source', 2, stream='other'),
    grouped('new language', 2, language='en'), TranslationInput(TranscriptionChunk('legacy'))])
def test_context_does_not_cross_source_language_or_protocol_changes(next_item):
    async def scenario():
        result = [x async for x in sentence_translation_inputs(values([grouped('context'), next_item]))]
        assert result[1].grouping['close_only']
        assert result[1].grouping['source_chunk_ids'] == []
        assert result[2].chunk.text == next_item.chunk.text
    asyncio.run(scenario())


def test_timeout_draft_is_replaced_using_whole_sentence_and_closed_once_at_stop():
    async def scenario():
        target = agent('es')
        await target.start()
        target._translation_max_wait_s = .015
        first = 'I en tercer lloc, hi ha una'
        second = 'altra qüestió que és interessant, però que afecta'
        responses = {first: 'En tercer lugar, hay una', first+' '+second:
                     'Y en tercer lugar, hay otra cuestión que es interesante, pero que afecta'}
        target.translator.translate_with_status.side_effect = lambda text, **kw: TranslationResult(responses[text])
        source = asyncio.Queue()
        async def chunks():
            while (chunk := await source.get()) is not None:
                yield chunk
        task = asyncio.create_task(target._process_chunk_stream(chunks()))
        try:
            await source.put(prefix(first))
            for _ in range(100):
                if len(target.room.local_participant.messages) >= 1: break
                await asyncio.sleep(.002)
            first_message = target.room.local_participant.messages[0]
            assert first_message['text'] == responses[first] and not first_message['revision_closed']
            assert first_message['is_partial'] and not first_message['is_final']  # Legacy viewers too.
            await source.put(prefix(second, 2))
            for _ in range(100):
                if len(target.room.local_participant.messages) >= 2: break
                await asyncio.sleep(.002)
            revised = target.room.local_participant.messages[1]
            assert revised['text'] == responses[first+' '+second] and not revised['revision_closed']
            assert revised['revision_key'] == first_message['revision_key']
            assert target._translation_confirmed == []
            await source.put(None)
            await asyncio.wait_for(task, 1)
            closed = target.room.local_participant.messages[-1]
            assert closed['revision_closed'] and closed['text'] == revised['text']
            assert closed['is_final'] and not closed['is_partial']
            assert closed['revision_key'] == revised['revision_key']
            assert closed['closure_reason'] == 'stream_end'
            assert target.translator.translate_with_status.await_count == 2  # Closure reuses the draft.
            assert closed['translation_group']['source_chunk_ids'] == []
            assert 'una Otra' not in closed['text'] and 'una otra' not in closed['text']
        finally:
            if not task.done(): task.cancel(); await asyncio.gather(task, return_exceptions=True)
            await target.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('language,text', [('es','La Clàudia trabaja en el BSC, en Barcelona.'),
                                         ('en','I work at the BBC in London.')])
def test_model_capitalization_of_names_acronyms_and_english_i_is_preserved(language,text):
    async def scenario():
        target = agent(language)
        await target.start()
        target.translator.translate_with_status.return_value = TranslationResult(text)
        target.translator.translate_with_status.side_effect = None
        await target._process_chunk_stream(values_chunks([prefix('text de prova.')]))
        assert target.room.local_participant.messages[-1]['text'] == text
        await target.stop()
    asyncio.run(scenario())


async def values_chunks(items):
    for item in items: yield item


def test_closing_fallback_preserves_language_status_and_does_not_repeat_request():
    async def scenario():
        target = agent('es')
        await target.start()
        target.translator.translate_with_status.side_effect = lambda text, **kw: TranslationResult(text, 'fallback', 'timeout')
        transformed = [x async for x in sentence_translation_inputs(values([grouped('source text')]))]
        for x in transformed:
            await target._handle_transcription_chunk(x.chunk, translation_only=True, translation_group=x.grouping)
        assert target.translator.translate_with_status.await_count == 1
        closed = target.room.local_participant.messages[-1]
        assert closed['revision_closed'] and closed['translation_fallback']
        assert closed['text_language'] == 'ca' and closed['translation_error'] == 'timeout'
        await target.stop()
    asyncio.run(scenario())


def test_provisional_translation_is_not_spoken_and_closure_speaks_only_once():
    async def scenario():
        target = agent('es')
        await target.start()
        target.audio_publisher = object()
        target._tts_text_buffer.add_chunk = Mock(return_value=None)
        transformed = [x async for x in sentence_translation_inputs(values([grouped('source text')]))]
        await target._handle_transcription_chunk(transformed[0].chunk, translation_only=True,
                                                translation_group=transformed[0].grouping)
        target._tts_text_buffer.add_chunk.assert_not_called()
        await target._handle_transcription_chunk(transformed[1].chunk, translation_only=True,
                                                translation_group=transformed[1].grouping)
        target._tts_text_buffer.add_chunk.assert_called_once()
        await target.stop()
    asyncio.run(scenario())


def test_successful_context_revision_replaces_fallback_without_stale_error():
    async def scenario():
        target = agent('es')
        await target.start()
        target.translator.translate_with_status.side_effect = [
            TranslationResult('text original', 'fallback', 'timeout'),
            TranslationResult('La Clàudia trabaja en el BSC.')]
        transformed = [x async for x in sentence_translation_inputs(values([
            grouped('text original'), grouped('continua.',2,'sentence')]))]
        for x in transformed:
            await target._handle_transcription_chunk(x.chunk, translation_only=True, translation_group=x.grouping)
        draft,closed=target.room.local_participant.messages
        assert draft['translation_fallback'] and not draft['revision_closed']
        assert closed['revision_closed'] and not closed['translation_fallback']
        assert closed['text_language']=='es' and closed['translation_error']==''
        assert draft['revision_key']==closed['revision_key']
        assert target._translation_confirmed==['La Clàudia trabaja en el BSC.']
        await target.stop()
    asyncio.run(scenario())


def test_context_switch_can_restore_previous_per_group_behavior():
    async def scenario():
        target = agent('es')
        await target.start()
        target._translation_sentence_context = False
        await target._process_chunk_stream(values_chunks([prefix('first sentence.'),prefix('second sentence.',2)]))
        assert all(x['revision_closed'] and 'sentence_key' not in x['translation_group']
                   for x in target.room.local_participant.messages)
        await target.stop()
    asyncio.run(scenario())
