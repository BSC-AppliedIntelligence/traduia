"""Runtime ASR failure must reach the session API, viewers and test clients."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import main
from asr.whisper_client import WhisperLiveClient, ASRStreamError
from test_caption_protocol import agent, chunk


class Socket:
    def __init__(self, messages): self.messages = messages
    def __aiter__(self):
        async def receive():
            for item in self.messages: yield json.dumps(item)
        return receive()


@pytest.mark.parametrize('protocol', [None, 'stable_text_prefix_v1'])
def test_context_error_is_not_a_model_load_error_and_reaches_consumer(protocol):
    async def scenario():
        c=WhisperLiveClient('unused',9090)
        ready={'message':'SERVER_READY','model':'model','caption_protocol':protocol}
        messages=[ready]
        if protocol:
            messages.append({'text_prefix':{'version':1,'sequence':1,'confirmed':[{'sequence':1,'text':'speech'}],
                                           'after_confirmed':1,'draft':''}})
        messages.append({'status':'ERROR','error_code':'prefix_context_lost','message':'private server details'})
        c._ws=Socket(messages);c._connected=True
        await c._receive_loop()
        with pytest.raises(ASRStreamError) as caught:
            _=[item async for item in c.receive_transcriptions()]
        assert caught.value.code=='prefix_context_lost'
        assert 'retained speech context' in str(caught.value)
        assert 'prepared' not in str(caught.value) and 'private' not in str(caught.value)
    asyncio.run(scenario())


@pytest.mark.parametrize('closing', [False, True])
def test_only_unexpected_ready_connection_end_is_failure(closing):
    async def scenario():
        c=WhisperLiveClient('unused',9090);c._connected=True;c._closing=closing
        c._ws=Socket([{'message':'SERVER_READY'}])
        await c._receive_loop()
        if closing:
            assert [item async for item in c.receive_transcriptions()]==[]
        else:
            with pytest.raises(ASRStreamError) as caught:
                _=[item async for item in c.receive_transcriptions()]
            assert caught.value.code=='asr_disconnected'
    asyncio.run(scenario())


def test_failure_is_persisted_and_published_after_an_earlier_valid_caption():
    async def scenario():
        owner=agent('original')
        await owner.start()
        owner.fanout_agents=[SimpleNamespace(target_language='es'),SimpleNamespace(target_language='en')]
        owner._fanout_source_chunk=AsyncMock()
        async def source():
            yield chunk('Valid speech.')
            raise ASRStreamError('prefix_context_lost')
        with patch('main.broadcast_log',AsyncMock()) as logs, \
             patch('main.captions_manager.broadcast',AsyncMock()) as broadcast, \
             patch.dict(main.event_id_to_code,{'caption-test':'TEST'}):
            await owner._process_chunk_stream(source())
        assert owner.asr_error['code']=='prefix_context_lost' and not owner.is_active
        control=[d for d in owner.room.local_participant.messages if d.get('type')=='asr_status']
        assert len(control)==1 and control[0]['status']=='failed'
        assert control[0]['stream_id']==owner._caption_stream_id
        assert logs.await_args.args[1]['asr_failed']
        channels={call.args[1] for call in broadcast.await_args_list if json.loads(call.args[2]).get('type')=='asr_status'}
        assert channels=={'original','es','en'}
        session=SimpleNamespace(source_asr_agent=owner,whisper_agents={},is_active=True,
            room_name='room',source_language='ca',target_languages=['es','en'])
        with patch.dict(main.active_sessions,{'caption-test':session}):
            status=await main.get_session_status('caption-test')
        assert status['active'] and status['asr_status']=='failed'
        assert status['asr_error']['code']=='prefix_context_lost'
    asyncio.run(scenario())


def test_failed_livekit_delivery_does_not_hide_failure_from_websocket_or_api():
    async def scenario():
        owner=agent('original');await owner.start()
        owner.room.local_participant.publish_data=AsyncMock(side_effect=ConnectionError('private connection'))
        with patch('main.broadcast_log',AsyncMock()),patch('main.captions_manager.broadcast',AsyncMock()) as send, \
             patch.dict(main.event_id_to_code,{'caption-test':'TEST'}):
            await owner._report_asr_failure(RuntimeError('private connection'))
        assert send.await_count==1 and owner.asr_error['code']=='asr_stream_failed'
        assert 'private' not in owner.asr_error['message']
    asyncio.run(scenario())
