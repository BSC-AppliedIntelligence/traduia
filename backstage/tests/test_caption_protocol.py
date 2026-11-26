import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from asr.whisper_client import TranscriptionChunk, WhisperLiveClient
from main import WhisperAgent


class Participant:
    def __init__(self):
        self.messages = []

    async def publish_data(self, payload, **kwargs):
        self.messages.append(json.loads(payload))


def agent(language="original"):
    room = type("Room", (), {"local_participant": Participant()})()
    return WhisperAgent(target_language=language, event_id="caption-test",
                        audio_publisher=None, room=room, is_primary_agent=language == "original")


def chunk(text, reason="stable", start=0, end=2, partial=False, identity="chunk-1"):
    return TranscriptionChunk(text=text, language="ca", is_partial=partial,
                              start_time=start, end_time=end, chunk_id=identity,
                              finalization_reason=reason)


def test_partial_timeout_and_corrected_final_metadata_match_both_transports():
    async def scenario():
        owner = agent()
        broadcast = AsyncMock()
        with patch.dict("main.event_id_to_code", {"caption-test": "TEST"}), \
             patch("main.captions_manager.broadcast", broadcast):
            await owner._handle_transcription_chunk(chunk("La reunió", partial=True))
            await owner._handle_transcription_chunk(chunk("La reunió és avui", "timeout", identity="timeout-1"))
            await owner._handle_transcription_chunk(chunk("La reunió és demà.", identity="stable-1"))
        messages = owner.room.local_participant.messages
        assert [m["revision_closed"] for m in messages] == [False, False, True]
        assert [m["finalization_reason"] for m in messages] == ["partial", "timeout", "stable"]
        assert messages[1]["is_final"] is True  # Legacy flag remains compatible.
        assert messages[1]["revision_key"] == messages[2]["revision_key"] != ""
        assert [m["caption_sequence"] for m in messages] == [1, 2, 3]
        assert [(m["audio_start_s"], m["audio_end_s"]) for m in messages] == [(0, 2)] * 3
        assert len({m["stream_id"] for m in messages}) == 1
        assert [json.loads(c.args[2]) for c in broadcast.await_args_list] == messages
        assert not owner._caption_drafts
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["stable", "eos"])
def test_non_timeout_finals_are_committed(reason):
    async def scenario():
        owner = agent()
        await owner._handle_transcription_chunk(chunk("Un fragment consolidat.", reason))
        assert owner.room.local_participant.messages[0]["revision_closed"] is True
    asyncio.run(scenario())


def test_explicit_prefix_is_closed_and_exposes_no_invented_audio_times():
    async def scenario():
        owner = agent()
        for partial in [True, False]:
            value = TranscriptionChunk('Un fragment consolidat.', language='ca',
                is_partial=partial, finalization_reason='stable_prefix',
                prefix_protocol=True, chunk_id='prefix:1')
            await owner._handle_transcription_chunk(value)
        messages = owner.room.local_participant.messages
        assert [m['revision_closed'] for m in messages] == [False, True]
        assert all(m['audio_start_s'] is None and m['audio_end_s'] is None for m in messages)
        assert messages[-1]['revision_key'] == ''
    asyncio.run(scenario())


def test_prefix_protocol_preserves_repetitions_through_source_and_translation_fanout():
    async def scenario():
        owner = agent()
        await owner.start()
        owner.whisper = WhisperLiveClient('unused', 9090)
        owner.whisper._server_info = {'caption_protocol': 'stable_text_prefix_v1'}
        target = WhisperAgent(target_language='es', event_id='caption-test',
                              audio_publisher=None, room=agent().room, owns_whisper=False)
        await target.start()
        # This is a protocol/repetition test of the ungrouped compatibility path.
        target._translation_max_wait_s = 0
        target.translator = type('Translator', (), {'translate': AsyncMock(return_value='una prueba')})()
        await target.start_source_chunk_consumer()
        owner.fanout_agents = [target]
        packet = {'text_prefix': {'version': 1, 'sequence': 1, 'after_confirmed': 2,
            'confirmed': [{'sequence': 1, 'text': 'una prova'}, {'sequence': 2, 'text': 'una prova'}],
            'draft': ''}}
        owner.whisper._receive_queue.put_nowait(packet)
        owner.whisper._receive_queue.put_nowait(packet)
        owner.whisper._receive_queue.put_nowait(None)
        await owner._process_transcriptions()
        for participant in [owner.room.local_participant, target.room.local_participant]:
            captions = [m for m in participant.messages if not m['is_partial']]
            assert len(captions) == 2
            assert all(m['revision_closed'] and m['audio_start_s'] is None
                       and m['audio_end_s'] is None for m in captions)
            assert len({m['chunk_id'] for m in captions}) == 2
        assert target.translator.translate.await_count == 2
        await owner.stop()
        await target.stop()
    asyncio.run(scenario())


def test_bounded_window_stays_revisable_until_retained_context_is_confirmed():
    async def scenario():
        owner = agent()
        await owner._handle_transcription_chunk(chunk("Un fragment amb context retingut.", "bounded_window"))
        draft, = owner.room.local_participant.messages
        assert draft["revision_closed"] is False
        assert draft["revision_key"]
        await owner._close_caption_revision("stream_end")
        assert owner.room.local_participant.messages[-1]["revision_closed"] is True
    asyncio.run(scenario())


def test_new_audio_span_commits_previous_timeout_without_replacing_it():
    async def scenario():
        owner = agent()
        await owner._handle_transcription_chunk(chunk("Primera frase", "timeout", identity="timeout-1"))
        await owner._handle_transcription_chunk(chunk("Una altra frase.", start=5, end=7, identity="stable-2"))
        draft, closed, next_final = owner.room.local_participant.messages
        assert closed["text"] == draft["text"]
        assert (closed["audio_start_s"], closed["audio_end_s"]) == (draft["audio_start_s"], draft["audio_end_s"])
        assert closed["revision_key"] == draft["revision_key"]
        assert closed["revision_closed"] is True
        assert closed["closure_reason"] == "next_chunk"
        assert next_final["revision_key"] == ""
        assert next_final["text"] == "Una altra frase."
    asyncio.run(scenario())


def test_stream_end_commits_latest_timeout_without_retranslation():
    async def scenario():
        target = agent("en")
        target.translator = type("Translator", (), {"translate": AsyncMock(return_value="The latest draft")})()
        await target._handle_transcription_chunk(chunk("L'últim esborrany", "timeout"))
        await target._flush_tts_and_log_end()
        await target._flush_tts_and_log_end()
        draft, closed = target.room.local_participant.messages
        assert closed["revision_key"] == draft["revision_key"]
        assert closed["revision_closed"] is True
        assert closed["closure_reason"] == "stream_end"
        assert closed["finalization_reason"] == "timeout"
        assert closed["text"] == draft["text"]
        target.translator.translate.assert_awaited_once()
    asyncio.run(scenario())


def test_partial_after_timeout_has_same_revision_key():
    async def scenario():
        owner = agent()
        await owner._handle_transcription_chunk(chunk("Una hipòtesi", "timeout"))
        await owner._handle_transcription_chunk(chunk("Una hipòtesi més llarga", partial=True))
        draft, partial = owner.room.local_participant.messages
        assert partial["revision_key"] == draft["revision_key"]
        assert partial["revision_closed"] is False
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["stable", "bounded_window", "eos"])
def test_expanded_timeout_reaches_original_and_shared_translations_once(reason):
    async def scenario():
        prefix = "Aquí ja començo destacant..."
        full = "Aquí ja començo destacant que la Clàudia Goldin va ser la primera dona"
        owner = agent()
        await owner.start()
        owner.whisper = WhisperLiveClient("unused", 9090, active_timeout_s=0.01)
        owner._timeout_revision_buffer_s = 0
        prefix_sent = asyncio.Event()
        send_subtitle = owner._send_subtitle

        async def observe_source(**payload):
            await send_subtitle(**payload)
            if payload["text"] == prefix and not payload["is_partial"]:
                prefix_sent.set()

        owner._send_subtitle = observe_source
        targets = []
        for language in ("en", "es"):
            target = WhisperAgent(target_language=language, event_id="caption-test",
                                  audio_publisher=None, room=agent().room, owns_whisper=False)
            await target.start()
            target.translator = type("Translator", (), {
                "translate": AsyncMock(side_effect=lambda text, lang=language: f"{lang}: {text.rstrip('.')}.")
            })()
            # Exercise TTS scheduling without constructing a provider client.
            target.audio_publisher = object()
            target._generate_tts = AsyncMock()
            await target.start_source_chunk_consumer()
            targets.append(target)
        owner.fanout_agents = targets

        async def feed():
            await owner.whisper._receive_queue.put({"segments": [
                {"text": prefix, "start": 42.699, "end": 44.621}
            ]})
            # Allow the initial hypothesis to reach the caption/translation
            # drafts before its longer stable confirmation arrives.
            await asyncio.wait_for(prefix_sent.wait(), timeout=1)
            expanded = {"segments": [{"text": full, "start": 42.699, "end": 47.189,
                                      "completed": True, "finalization_reason": reason}]}
            await owner.whisper._receive_queue.put(expanded)
            await owner.whisper._receive_queue.put(expanded)
            await owner.whisper._receive_queue.put(None)

        await asyncio.gather(feed(), owner._process_transcriptions())
        original = [m for m in owner.room.local_participant.messages if not m["is_partial"]]
        expected_texts = [prefix, full, full] if reason == "bounded_window" else [prefix, full]
        expected_closed = [False, False, True] if reason == "bounded_window" else [False, True]
        assert [m["text"] for m in original] == expected_texts
        assert [m["revision_closed"] for m in original] == expected_closed
        assert original[0]["revision_key"] == original[1]["revision_key"] != ""
        for target in targets:
            translated = target.room.local_participant.messages
            assert [m["text"] for m in translated] == [
                f"{target.target_language}: {text.rstrip('.')}." for text in expected_texts
            ]
            assert [m["revision_closed"] for m in translated] == expected_closed
            assert translated[0]["revision_key"] == translated[1]["revision_key"] != ""
            assert [m["chunk_id"] for m in translated] == [m["chunk_id"] for m in original]
            assert target.translator.translate.await_count == 2
            # Neither the repeated wire message nor stream-end closure
            # schedules another translation/TTS of the corrected result.
            assert target._generate_tts.await_count == 2
            assert not target._caption_drafts
        assert not owner._caption_drafts
    asyncio.run(scenario())


@pytest.mark.parametrize("example", ["particular", "coneixement", "opening", "elinor",
                                      "ordinary-phrase", "ordinary-fragment", "ordinary-spelling", "long-tail"])
def test_overlapping_corrections_form_one_caption_per_source_span_in_all_languages(example):
    async def scenario():
        translations = 2
        if example == "particular":
            first = "I són els coneixements en aquestes dues branques el que fan tan particular la seva recerca, com veurem a continuació."
            tail = "en particular, a la seva recerca, com veurem a continuació."
            incoming = [chunk(first, "bounded_window", 57.261, 66.841, identity="window"),
                        chunk(tail, "timeout", 62.497, 66.858, identity="tail-timeout"),
                        chunk(tail, "stable", 62.497, 66.858, identity="tail-stable")]
            expected = ["I són els coneixements en aquestes dues branques el que fan en particular, a la seva recerca, com veurem a continuació."]
        elif example == "coneixement":
            first = "La Clàudia Golding guanya el Premi Nobel per avançar el nostre coneixement."
            second = "en els resultats de les dones al mercat de treball."
            incoming = [chunk(first, "timeout", 138.751, 145.251, identity="first"),
                        chunk(second, "bounded_window", 145.291, 148.391, identity="second"),
                        chunk("el nostre coneixement " + second, "timeout", 143.482, 148.502, identity="overlap")]
            expected = [first, second]
        elif example == "opening":
            first = "Al final, i això també ho hem pactat amb el Jordi, parlaré una mica de l'estat de la qüestió en la professió, sobre quin és l'estat de la qüestió matèria."
            incoming = [chunk(first, "bounded_window", 4.780, 14.221, identity="window"),
                        chunk("la qüestió en la", "stable", 9.8, 11.201, identity="interior-1"),
                        chunk("professió, sobre quin és", "stable", 11.201, 13.001, identity="interior-2"),
                        chunk("la qüestió matèria de gènere.", "timeout", 13.001, 14.921, identity="tail"),
                        chunk("de la qüestió matèria de gènere.", "stable", 13.001, 14.941, identity="confirmed")]
            expected = [first[:-1] + " de gènere."]
        elif example == "elinor":
            incoming = [chunk("La primera va ser l'any 2009.", "timeout", 72.876, 75.156, identity="year"),
                        chunk("La primera va ser l'any 2009, la Elinor Om.", "timeout", 72.876, 76.596, identity="fragment"),
                        chunk("l'Elinor Armstrong, que juntament amb l'Oliver Witt", "timeout", 75.716, 78.783, identity="expanded"),
                        chunk("la Elinor Armstrong, que juntament amb l'Oliver Williamson", "stable", 75.316, 79.476, identity="confirmed")]
            expected = ["La primera va ser l'any 2009, la Elinor Armstrong, que juntament amb l'Oliver Williamson"]
            translations = 4
        elif example == "long-tail":
            first = "i, o dit d'altra manera, per posar el gènere i les dones com a objecte de la recerca."
            tail = 'com a objecte de la recerca en economia.'
            incoming = [chunk(first, 'timeout', 148.841, 154.581, identity='first'),
                        chunk(tail, 'partial', 152.461, 155.881, partial=True, identity='partial'),
                        chunk(tail, 'timeout', 152.461, 155.881, identity='tail'),
                        chunk(tail, 'stable', 152.461, 155.881, identity='confirmed')]
            expected = [first[:-1] + ' en economia.']
        else:
            first, tail, corrected = {
                "ordinary-phrase": ("Aquest estudi analitza el mercat laboral.",
                                    "mercat laboral de les dones.",
                                    "Aquest estudi analitza el mercat laboral de les dones."),
                "ordinary-fragment": ("La proposta incorpora tecnolo.",
                                      "tecnologies aplicades a la recerca.",
                                      "La proposta incorpora tecnologies aplicades a la recerca."),
                "ordinary-spelling": ("Parlem del mercat labural.",
                                      "mercat laboral i dels salaris.",
                                      "Parlem del mercat laboral i dels salaris."),
            }[example]
            incoming = [chunk(first, "timeout", 0, 5, identity="first"),
                        chunk(tail, "partial", 4, 8, partial=True, identity="partial"),
                        chunk(tail, "timeout", 4, 8, identity="expanded"),
                        chunk(tail, "stable", 4, 8, identity="confirmed"),
                        chunk(tail, "stable", 4, 8, identity="confirmed")]
            expected = [corrected]

        owner = agent()
        await owner.start()
        targets = []
        for language in ("en", "es"):
            target = WhisperAgent(target_language=language, event_id="caption-test",
                                  audio_publisher=None, room=agent().room, owns_whisper=False)
            target.translator = type("Translator", (), {
                "translate": AsyncMock(side_effect=lambda text, lang=language: f"{lang}: {text}")
            })()
            await target.start()
            await target.start_source_chunk_consumer()
            targets.append(target)
        owner.fanout_agents = targets

        async def stream():
            for item in incoming:
                yield item

        await owner._process_chunk_stream(stream())
        await owner._finish_fanout_agents()
        original = [m for m in owner.room.local_participant.messages if m["revision_closed"]]
        assert [m["text"] for m in original] == expected
        for target in targets:
            translated = [m for m in target.room.local_participant.messages if m["revision_closed"]]
            assert [m["text"] for m in translated] == [f"{target.target_language}: {text}" for text in expected]
            assert target.translator.translate.await_count == translations
            assert [(m["audio_start_s"], m["audio_end_s"]) for m in translated] == [
                (m["audio_start_s"], m["audio_end_s"]) for m in original
            ]
    asyncio.run(scenario())


def test_unchanged_confirmation_does_not_repeat_translation_or_tts():
    async def scenario():
        target = agent("en")
        target.translator = type("Translator", (), {"translate": AsyncMock(return_value="The same confirmed caption.")})()
        target.audio_publisher = object()
        target._generate_tts = AsyncMock()
        await target._handle_transcription_chunk(chunk("Un text sense canvis.", "bounded_window"))
        await target._handle_transcription_chunk(chunk("Un text sense canvis.", "stable"))
        await target._flush_tts_and_log_end()
        draft, closed = target.room.local_participant.messages
        assert not draft["revision_closed"] and closed["revision_closed"]
        assert closed["closure_reason"] == "confirmed"
        assert closed["finalization_reason"] == "stable"
        target.translator.translate.assert_awaited_once()
        target._generate_tts.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["timeout", "bounded_window", "stable"])
def test_translation_fallback_metadata_matches_transports_and_survives_closure(reason):
    from translation.aina_translator import AinaTranslator
    import httpx

    async def scenario():
        target = agent("es")
        target.translator = AinaTranslator("http://translator.invalid", "es")
        client = type("Client", (), {"post": AsyncMock(side_effect=httpx.ReadError(""))})()
        target.translator._get_client = AsyncMock(return_value=client)
        broadcast = AsyncMock()
        with patch.dict("main.event_id_to_code", {"caption-test": "TEST"}), \
             patch("main.captions_manager.broadcast", broadcast), \
             patch("main.broadcast_log", AsyncMock()) as terminal_log:
            await target._handle_transcription_chunk(chunk("El text original.", reason))
            await target._close_caption_revision("stream_end")
        messages = target.room.local_participant.messages
        assert len(messages) == (1 if reason == "stable" else 2)
        for message in messages:
            assert message["channel"] == message["language"] == "es"
            assert message["text"] == "El text original."
            assert message["text_language"] == "ca"
            assert message["translation_status"] == "fallback"
            assert message["translation_fallback"] is True
            assert message["translation_error"] == "request_error"
        assert messages[-1]["revision_closed"] is True
        assert [json.loads(c.args[2]) for c in broadcast.await_args_list] == messages
        assert client.post.await_count == 1
        assert any(c.args[1].get("level") == "warning" for c in terminal_log.await_args_list)
    asyncio.run(scenario())


def test_translation_failure_does_not_taint_the_next_successful_caption():
    from translation.base import TranslationResult

    async def scenario():
        target = agent("es")
        target.translator = type("Translator", (), {"translate_with_status": AsyncMock(side_effect=[
            TranslationResult("Original català.", "fallback", "timeout"),
            TranslationResult("Traducción correcta."),
        ])})()
        await target._handle_transcription_chunk(chunk("Original català."))
        await target._handle_transcription_chunk(chunk("Següent text.", start=3, end=5, identity="chunk-2"))
        failed, success = target.room.local_participant.messages
        assert failed["translation_fallback"] is True
        assert success["translation_fallback"] is False
        assert success["translation_status"] == "translated"
        assert success["text_language"] == "es" and success["translation_error"] == ""
    asyncio.run(scenario())


@pytest.mark.parametrize("available", [True, False])
def test_raising_or_missing_translator_marks_original_as_fallback(available):
    async def scenario():
        target = agent("es")
        if available:
            target.translator = type("Translator", (), {
                "translate": AsyncMock(side_effect=RuntimeError(""))})()
        await target._handle_transcription_chunk(chunk("Un text original."))
        message, = target.room.local_participant.messages
        assert message["translation_status"] == "fallback"
        assert message["translation_error"] == ("translator_error" if available else "translator_unavailable")
    asyncio.run(scenario())


def test_pause_recovery_reason_is_preserved_and_closed_through_the_caption_protocol():
    async def scenario():
        client=WhisperLiveClient('unused',9090)
        packet={'text_prefix':{'version':1,'sequence':1,'after_confirmed':1,'draft':'',
            'confirmed':[{'sequence':1,'text':'Un fragment complet.',
                          'finalization_reason':'vad_pause_recovery'}]}}
        client._receive_queue.put_nowait(None)
        chunks=[c async for c in client._receive_text_prefix_transcriptions(packet)]
        assert len(chunks)==1 and chunks[0].finalization_reason=='vad_pause_recovery'
        owner=agent();await owner._handle_transcription_chunk(chunks[0])
        payload=owner.room.local_participant.messages[-1]
        assert payload['revision_closed'] and payload['finalization_reason']=='vad_pause_recovery'
    asyncio.run(scenario())


def test_prefix_closure_reason_cannot_be_changed_after_publication():
    async def scenario():
        client=WhisperLiveClient('unused',9090)
        first={'text_prefix':{'version':1,'sequence':1,'after_confirmed':1,'draft':'',
                             'confirmed':[{'sequence':1,'text':'same text'}]}}
        second={'text_prefix':{'version':1,'sequence':2,'after_confirmed':1,'draft':'',
            'confirmed':[{'sequence':1,'text':'same text','finalization_reason':'vad_pause_recovery'}]}}
        client._receive_queue.put_nowait(second);client._receive_queue.put_nowait(None)
        with pytest.raises(ValueError,match='Closed ASR prefix was revised'):
            _=[c async for c in client._receive_text_prefix_transcriptions(first)]
    asyncio.run(scenario())
