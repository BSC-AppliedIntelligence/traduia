import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asr.whisper_client import TranscriptionChunk
from main import (
    TranslationSession,
    WhisperAgent,
    needs_primary_transcription_agent,
    start_session_agents,
)


class DummyPublisher:
    pass


class DummyTranslator:
    def __init__(self, base_url="", target_language="en", source_language="ca"):
        self.base_url = base_url
        self.target_language = target_language
        self.source_language = source_language

    async def translate(self, text):
        return f"{self.target_language}:{text}"


class DummyParticipant:
    def __init__(self):
        self.messages = []

    async def publish_data(self, payload, reliable=True, topic=""):
        self.messages.append({
            "payload": json.loads(payload.decode()),
            "reliable": reliable,
            "topic": topic,
        })


class DummyRoom:
    def __init__(self):
        self.local_participant = DummyParticipant()


def _chunk(text, partial=False, chunk_id="chunk-1"):
    return TranscriptionChunk(
        text=text,
        language="ca",
        is_partial=partial,
        start_time=0.0,
        end_time=1.0,
        finalization_reason="stable",
        chunk_id=chunk_id,
    )


def _run(coro):
    return asyncio.run(coro)


def test_start_session_agents_uses_one_shared_whisper_owner():
    async def scenario():
        connects = []
        consumers = []

        async def fake_start(self):
            self.is_running = True

        async def fake_connect(self):
            connects.append(self.target_language)

        async def fake_start_consumer(self):
            consumers.append(self.target_language)

        session = TranslationSession(
            event_id="event-1",
            room_name="room-1",
            source_language="es",
            target_languages=["es", "en"],
            room=DummyRoom(),
            whisper_config={
                "host": "whisperlive",
                "port": 9090,
                "model": "small",
                "language": "ca",
                "translator_url": "http://translator:8000",
            },
        )
        session.audio_publishers = {
            "es": DummyPublisher(),
            "en": DummyPublisher(),
        }

        with (
            patch("translation.aina_translator.AinaTranslator", DummyTranslator),
            patch("main.SHARED_ASR_ENABLED", True),
            patch.object(WhisperAgent, "start", fake_start),
            patch.object(WhisperAgent, "connect_whisper", fake_connect),
            patch.object(WhisperAgent, "start_source_chunk_consumer", fake_start_consumer),
        ):
            await start_session_agents(session)

        assert connects == ["original"]
        assert consumers == ["en"]
        assert session.source_asr_agent is not None
        assert session.source_asr_agent.owns_whisper is True
        assert session.source_asr_agent.is_primary_agent is True
        assert session.source_asr_agent.target_language == "original"
        assert session.source_asr_agent.fanout_agents == [
            session.whisper_agents["es"],
            session.whisper_agents["en"],
        ]
        assert session.whisper_agents["es"].owns_whisper is False
        assert session.whisper_agents["es"].is_active is False
        assert session.whisper_agents["en"].owns_whisper is False
        assert session.whisper_agents["en"].is_active is True

    _run(scenario())


def test_start_session_agents_can_use_duplicated_whisper_owners():
    async def scenario():
        connects = []
        consumers = []

        async def fake_start(self):
            self.is_running = True

        async def fake_connect(self):
            connects.append(self.target_language)

        async def fake_start_consumer(self):
            consumers.append(self.target_language)

        session = TranslationSession(
            event_id="event-duplicated",
            room_name="room-duplicated",
            source_language="ca",
            target_languages=["es", "en"],
            room=DummyRoom(),
            whisper_config={
                "host": "whisperlive",
                "port": 9090,
                "model": "small",
                "language": "ca",
                "translator_url": "http://translator:8000",
            },
        )
        session.audio_publishers = {
            "es": DummyPublisher(),
            "en": DummyPublisher(),
        }

        with (
            patch("translation.aina_translator.AinaTranslator", DummyTranslator),
            patch("main.SHARED_ASR_ENABLED", False),
            patch.object(WhisperAgent, "start", fake_start),
            patch.object(WhisperAgent, "connect_whisper", fake_connect),
            patch.object(WhisperAgent, "start_source_chunk_consumer", fake_start_consumer),
        ):
            await start_session_agents(session)

        assert connects == ["es", "en"]
        assert consumers == []
        assert session.source_asr_agent is None
        assert all(agent.owns_whisper for agent in session.whisper_agents.values())
        assert session.whisper_agents["es"].is_primary_agent is True
        assert session.whisper_agents["en"].is_primary_agent is False

    _run(scenario())


def test_duplicated_mode_skips_passthrough_target_connection():
    async def scenario():
        connects = []

        async def fake_start(self):
            self.is_running = True

        async def fake_connect(self):
            connects.append(self.target_language)

        session = TranslationSession(
            event_id="event-passthrough",
            room_name="room-passthrough",
            source_language="es",
            target_languages=["es", "en"],
            room=DummyRoom(),
            whisper_config={
                "host": "whisperlive",
                "port": 9090,
                "model": "small",
                "language": "ca",
                "translator_url": "http://translator:8000",
            },
        )
        session.audio_publishers = {
            "es": DummyPublisher(),
            "en": DummyPublisher(),
        }

        with (
            patch("translation.aina_translator.AinaTranslator", DummyTranslator),
            patch("main.SHARED_ASR_ENABLED", False),
            patch.object(WhisperAgent, "start", fake_start),
            patch.object(WhisperAgent, "connect_whisper", fake_connect),
        ):
            await start_session_agents(session)

        assert connects == ["en"]
        assert session.whisper_agents["es"].is_active is False
        assert session.whisper_agents["es"].is_primary_agent is False
        assert session.whisper_agents["en"].is_primary_agent is True

    _run(scenario())


def test_duplicated_targets_do_not_create_third_primary_asr_agent():
    session = TranslationSession(event_id="event", room_name="room")
    session.whisper_agents = {"en": object(), "es": object()}

    assert needs_primary_transcription_agent(session, {"host": "whisperlive"}) is False


def test_original_only_session_still_creates_primary_asr_agent():
    session = TranslationSession(event_id="event", room_name="room")

    assert needs_primary_transcription_agent(session, {"host": "whisperlive"}) is True


def test_shared_asr_fanout_sends_only_final_chunks_to_active_targets():
    async def scenario():
        source = WhisperAgent(
            target_language="original",
            event_id="event-1",
            audio_publisher=None,
            room=None,
            owns_whisper=True,
        )
        first = WhisperAgent(
            target_language="es",
            event_id="event-1",
            audio_publisher=None,
            room=None,
            owns_whisper=False,
        )
        second = WhisperAgent(
            target_language="en",
            event_id="event-1",
            audio_publisher=None,
            room=None,
            owns_whisper=False,
        )
        first_seen = []
        second_seen = []

        async def first_enqueue(chunk):
            first_seen.append(chunk)

        async def second_enqueue(chunk):
            second_seen.append(chunk)

        first.enqueue_source_chunk = first_enqueue
        second.enqueue_source_chunk = second_enqueue
        source.fanout_agents = [first, second]

        await source._fanout_source_chunk(_chunk("partial", partial=True))
        assert first_seen == []
        assert second_seen == []

        final = _chunk("final text")
        await source._fanout_source_chunk(final)
        assert first_seen == [final]
        assert second_seen == [final]

        second.is_active = False
        next_final = _chunk("next", chunk_id="chunk-2")
        await source._fanout_source_chunk(next_final)
        assert first_seen == [final, next_final]
        assert second_seen == [final]

    _run(scenario())


def test_shared_target_queue_translates_final_chunk_to_target_channel():
    async def scenario():
        room = DummyRoom()
        agent = WhisperAgent(
            target_language="en",
            event_id="event-1",
            audio_publisher=None,
            room=room,
            translator=DummyTranslator(target_language="en"),
            owns_whisper=False,
        )
        await agent.start()
        await agent.start_source_chunk_consumer()
        await agent.enqueue_source_chunk(_chunk("hola mon"))
        await agent.finish_source_chunks()

        messages = room.local_participant.messages
        assert len(messages) == 1
        assert messages[0]["topic"] == "subtitle.en"
        assert messages[0]["payload"]["text"] == "en:hola mon"
        assert messages[0]["payload"]["is_translation"] is True

    _run(scenario())


if __name__ == "__main__":
    test_start_session_agents_uses_one_shared_whisper_owner()
    test_start_session_agents_can_use_duplicated_whisper_owners()
    test_duplicated_mode_skips_passthrough_target_connection()
    test_duplicated_targets_do_not_create_third_primary_asr_agent()
    test_original_only_session_still_creates_primary_asr_agent()
    test_shared_asr_fanout_sends_only_final_chunks_to_active_targets()
    test_shared_target_queue_translates_final_chunk_to_target_channel()
