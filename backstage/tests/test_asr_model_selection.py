import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from asr.model_selection import ModelControlError, model_request
from asr.whisper_client import WhisperLiveClient

DEFAULT = "Systran/faster-whisper-large-v3"
SMALL = "Systran/faster-whisper-small"


class ControlSocket:
    def __init__(self, reply):
        self.reply, self.sent = reply, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        return json.dumps(self.reply)


def test_control_roundtrip_and_conflict_mapping():
    async def run():
        socket = ControlSocket({'type':'asr_model_control', 'ok':True,
                                'state':{'enabled':True, 'models':[], 'selected_model':SMALL}})
        with patch('asr.model_selection.websockets.connect',return_value=socket):
            state = await model_request({'host':'whisperlive','port':9090},'select',model=SMALL,revision=3)
        assert state['selected_model']==SMALL
        assert socket.sent==[{'type':'asr_model_control','operation':'select','model':SMALL,'expected_revision':3}]
        socket.reply={'type':'asr_model_control','ok':False,'error':'selection_conflict','message':'private upstream text'}
        with patch('asr.model_selection.websockets.connect',return_value=socket):
            with pytest.raises(ModelControlError) as failure:
                await model_request({'host':'whisperlive','port':9090})
        assert failure.value.status==409
        assert 'private' not in str(failure.value)
    asyncio.run(run())


def test_control_unavailable_does_not_expose_connection_errors():
    async def run():
        with patch('asr.model_selection.websockets.connect',side_effect=RuntimeError('private connection URL')):
            with pytest.raises(ModelControlError) as failure:
                await model_request({'host':'whisperlive','port':9090})
        assert failure.value.code=='control_unavailable'
        assert 'private' not in str(failure.value)
    asyncio.run(run())


def test_event_config_error_is_sanitized_for_the_model_panel():
    async def run():
        with patch.dict(os.environ, {'WHISPER_MODEL_SELECTION_REQUIRED':'true'}), \
             patch.object(main, 'get_event_endpoints', AsyncMock(side_effect=main.HTTPException(500, 'private provider detail'))):
            with pytest.raises(main.HTTPException) as failure:
                await main.get_shared_asr_model('A87729')
        assert failure.value.status_code == 500
        assert 'private' not in failure.value.detail
    asyncio.run(run())


def test_any_event_page_targets_its_saved_service_and_selection_revision():
    async def run():
        config=AsyncMock(return_value={'host':'shared-asr','port':9090})
        control=AsyncMock(return_value={'enabled':True,'selected_model':SMALL})
        with patch.dict(os.environ, {'WHISPER_MODEL_SELECTION_REQUIRED':'true'}), \
             patch.object(main,'get_event_endpoints',config), patch('asr.model_selection.model_request',control):
            await main.save_shared_asr_model('A87729',{'model':SMALL,'selection_revision':2})
            other=await main.get_shared_asr_model('OTHER')
        assert other['selected_model']==SMALL
        assert other['endpoint']=={'host':'shared-asr','port':9090}
        assert [call.args[0] for call in config.await_args_list]==['A87729','OTHER']
        assert control.await_args_list[0].kwargs=={'model':SMALL,'revision':2}
    asyncio.run(run())


def test_endpoint_only_save_preserves_legacy_model_and_does_not_select_globally():
    async def run():
        database=Mock()
        database.rpc.return_value.execute.return_value.data={}
        with patch.object(main,'supabase',database), patch.object(main,'get_event_endpoints',AsyncMock(return_value={'model':DEFAULT})):
            await main.save_event_endpoints('A87729',{'host':'new-asr','port':9090,'translator_url':'http://translator'})
        config=database.rpc.call_args.args[1]['p_config']
        assert config['host']=='new-asr'
        assert config['model']==DEFAULT
    asyncio.run(run())


def test_model_error_and_busy_are_reported_immediately_before_ready():
    async def run():
        client=WhisperLiveClient('asr',9090)
        client._connected=True
        client._server_error='ASR is busy or a model change is pending.'
        with pytest.raises(ConnectionError,match='pending'):
            await client.wait_until_ready(timeout=30)
    asyncio.run(run())


def fake_whisper():
    return SimpleNamespace(connect=AsyncMock(), close=AsyncMock(),
        wait_until_ready=AsyncMock(return_value={'model':SMALL,'device':'cuda'}))


def test_preparation_failure_closes_connection_and_leaves_broadcast_unstarted():
    async def run():
        client=fake_whisper()
        client.wait_until_ready.side_effect=ConnectionError('model change is pending')
        with patch.object(main,'make_whisper_client',return_value=client), patch.object(main,'broadcast_log',AsyncMock()):
            with pytest.raises(main.HTTPException) as failure:
                await main.prepare_whisper_client({'host':'asr','port':9090},'event')
        assert failure.value.status_code==409
        client.close.assert_awaited_once()
    asyncio.run(run())


def test_source_agent_reuses_prepared_connection_instead_of_loading_twice():
    async def run():
        client=fake_whisper()
        session=main.TranslationSession('event','room',whisper_config={'host':'asr','port':9090},prepared_whisper=client)
        agent=main.create_source_asr_agent(session)
        assert session.prepared_whisper is None
        with patch.object(main,'make_whisper_client') as factory, patch.object(main,'broadcast_log',AsyncMock()), \
             patch.object(agent,'_process_transcriptions',AsyncMock()):
            await agent.start()
            await agent.connect_whisper()
            await asyncio.sleep(0)
        factory.assert_not_called()
        client.connect.assert_not_awaited()
        assert agent.whisper is client
        await agent.stop()
    asyncio.run(run())


def session_context():
    database=Mock()
    database.rpc.return_value.execute.return_value.data={'host':'asr','port':9090,'model':'small'}
    return database


def test_create_failure_before_model_readiness_does_not_create_room_or_session():
    async def run():
        request=main.CreateSessionRequest(event_id='model-failure',event_title='Test',event_code='A87729',channels=[])
        ensure_room=AsyncMock()
        with patch.object(main,'LIVEKIT_READY',True), patch.object(main,'supabase',session_context()), \
             patch.object(main,'broadcast_log',AsyncMock()), patch.object(main,'ensure_room_exists',ensure_room), \
             patch('websockets.connect',AsyncMock(return_value=SimpleNamespace(close=AsyncMock()))), \
             patch.object(main,'prepare_whisper_client',AsyncMock(side_effect=main.HTTPException(503,'Could not load model'))):
            with pytest.raises(main.HTTPException):
                await main.create_session(request)
        assert request.event_id not in main.active_sessions
        ensure_room.assert_not_awaited()
    asyncio.run(run())


def test_failed_livekit_setup_releases_the_prepared_model_connection():
    async def run():
        request=main.CreateSessionRequest(event_id='livekit-failure',event_title='Test',event_code='A87729',channels=[])
        client=fake_whisper()
        with patch.object(main,'LIVEKIT_READY',True), patch.object(main,'supabase',session_context()), \
             patch.object(main,'broadcast_log',AsyncMock()), patch.object(main,'ensure_room_exists',AsyncMock()), \
             patch('websockets.connect',AsyncMock(return_value=SimpleNamespace(close=AsyncMock()))), \
             patch.object(main,'prepare_whisper_client',AsyncMock(return_value=client)), \
             patch.object(main,'setup_session_livekit',AsyncMock(side_effect=RuntimeError('private diagnostic'))):
            with pytest.raises(main.HTTPException) as failure:
                await main.create_session(request)
        assert request.event_id not in main.active_sessions
        assert 'private' not in str(failure.value.detail)
        client.close.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('saved_host', ['asr', 'another-alias'])
def test_pending_change_cannot_pass_a_live_event_whose_audio_socket_has_closed(saved_host):
    async def run():
        session = main.TranslationSession('idle-audio', 'room', is_active=True,
            whisper_config={'host':saved_host,'port':9090}, asr_service_id='same-service')
        state = {'pending':True,'service_id':'same-service','active_sessions':0}
        main.active_sessions[session.event_id] = session
        try:
            with patch.dict(os.environ, {'WHISPER_MODEL_SELECTION_REQUIRED':'true'}), \
                 patch('asr.model_selection.model_request', AsyncMock(return_value=state)), \
                 patch.object(main, 'make_whisper_client') as factory:
                with pytest.raises(main.HTTPException) as failure:
                    await main.prepare_whisper_client({'host':'asr','port':9090},'new-event')
            assert failure.value.status_code == 409
            factory.assert_not_called()
        finally:
            main.active_sessions.pop(session.event_id, None)
    asyncio.run(run())


def test_managed_start_requires_control_status_but_unchanged_model_allows_parallel_events():
    async def run():
        with patch.dict(os.environ, {'WHISPER_MODEL_SELECTION_REQUIRED':'true'}):
            with patch('asr.model_selection.model_request', AsyncMock(return_value={'pending':False,'service_id':'same'})):
                await main.ensure_shared_model_admission({'host':'asr','port':9090})
            with patch('asr.model_selection.model_request', AsyncMock(side_effect=ModelControlError('unavailable','Status unavailable.'))):
                with pytest.raises(main.HTTPException) as failure:
                    await main.ensure_shared_model_admission({'host':'asr','port':9090})
                assert failure.value.status_code == 503
    asyncio.run(run())


def test_reconnect_cannot_silently_change_an_existing_broadcast_model():
    async def run():
        client = fake_whisper()
        session = main.TranslationSession('model-mismatch', 'room', prepared_whisper=client)
        agent = main.create_source_asr_agent(session)
        with patch.object(main, 'broadcast_log', AsyncMock()), patch.object(agent, '_process_transcriptions', AsyncMock()):
            await agent.start()
            await agent.connect_whisper()
            client.wait_until_ready.return_value = {'model':DEFAULT,'device':'cuda'}
            with pytest.raises(ModelControlError, match='Stop and restart'):
                await agent.connect_whisper()
            assert agent._asr_model_id == SMALL
            assert agent.whisper is None
            client.close.assert_awaited_once()
            await agent.stop()
    asyncio.run(run())


def test_fixed_model_profile_refuses_model_management_without_contacting_asr():
    async def run():
        with patch('asr.model_selection.model_request', AsyncMock()) as control:
            with pytest.raises(main.HTTPException, match='not enabled'):
                await main.save_shared_asr_model('A87729', {'model':SMALL,'selection_revision':0})
        control.assert_not_awaited()
    asyncio.run(run())
