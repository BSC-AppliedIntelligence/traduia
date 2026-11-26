"""Start/cancellation regression checks without LiveKit, GPU or database I/O."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from test_asr_model_selection import fake_whisper, session_context


def test_cancelled_preparation_closes_its_asr_connection():
    async def run():
        client = fake_whisper()
        client.wait_until_ready.side_effect = asyncio.CancelledError()
        with patch.object(main, 'make_whisper_client', return_value=client), \
             patch.object(main, 'broadcast_log', AsyncMock()):
            with pytest.raises(asyncio.CancelledError):
                await main.prepare_whisper_client({'host': 'asr', 'port': 9090}, 'cancelled-event')
        client.close.assert_awaited_once()
    asyncio.run(run())


def test_two_start_clicks_and_stop_create_and_close_only_one_asr_connection():
    async def run():
        request = main.CreateSessionRequest(event_id='concurrent-start', event_title='Test',
                                           event_code='A87729', channels=[])
        client = fake_whisper()
        entered, finish = asyncio.Event(), asyncio.Event()
        async def prepare(*args):
            entered.set()
            await finish.wait()
            return client
        prepare_mock = AsyncMock(side_effect=prepare)
        livekit = SimpleNamespace(room=SimpleNamespace(delete_room=AsyncMock()), aclose=AsyncMock())
        with patch.object(main, 'LIVEKIT_READY', True), patch.object(main, 'supabase', session_context()), \
             patch.object(main, 'broadcast_log', AsyncMock()), patch.object(main, 'ensure_room_exists', AsyncMock()), \
             patch('websockets.connect', AsyncMock(return_value=type('Socket', (), {'close': AsyncMock()})())), \
             patch.object(main, 'prepare_whisper_client', prepare_mock), \
             patch.object(main, 'setup_session_livekit', AsyncMock()), \
             patch.object(main, 'get_livekit_api', AsyncMock(return_value=livekit)), \
             patch.object(main, 'create_token', return_value='local-test-token'), \
             patch.object(main.WhisperAgent, '_process_transcriptions', AsyncMock()):
            first = asyncio.create_task(main.create_session(request))
            await asyncio.wait_for(entered.wait(), 2)
            second = asyncio.create_task(main.create_session(request))
            stopped = asyncio.create_task(main.stop_session(request.event_id))
            await asyncio.sleep(0)
            assert prepare_mock.await_count == 1
            assert not stopped.done()  # Stop waits for bootstrap, not a false 404.
            finish.set()
            a, b, stop_result = await asyncio.wait_for(asyncio.gather(first, second, stopped), 3)
            assert a.session_id == b.session_id == request.event_id
            assert prepare_mock.await_count == 1
            assert stop_result['status'] == 'stopped'
            assert request.event_id not in main.active_sessions
            client.close.assert_awaited_once()
    asyncio.run(run())
