"""Real WebSocket admission/lifecycle tests with model loading replaced, no GPU."""
import functools
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from websockets.sync.client import connect
from websockets.sync.server import serve

from whisper_live.backend.faster_whisper_backend import ServeClientFasterWhisper
from whisper_live.model_selection import DEFAULT_MODEL, MODELS, SharedModelSelection
from whisper_live.server import BackendType, ClientManager, TranscriptionServer


class FakeModel:
    def __init__(self, name, events):
        self.name, self.events = name, events
        self.model = self

    def unload_model(self):
        self.events.append(('unload', self.name))


class ServerModelSelectionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.events = []
        self.drain = threading.Event()
        self.drain.set()
        self.server = TranscriptionServer()
        self.server.client_manager = ClientManager()
        self.server.cache_path = folder.name
        self.server.single_model = True
        self.server.model_selection = SharedModelSelection(Path(folder.name)/'selection.json')
        def load(client, device):
            self.events.append(('load', client.model_size_or_path))
            client.transcriber = FakeModel(client.model_size_or_path, self.events)
        def infer(client):
            while not client.exit:
                time.sleep(.01)
            self.drain.wait(5)
        for target, replacement in [('create_model', load), ('speech_to_text', infer)]:
            context = patch.object(ServeClientFasterWhisper, target, replacement)
            context.start()
            self.addCleanup(context.stop)
        handler = functools.partial(self.server.recv_audio, backend=BackendType.FASTER_WHISPER,
                                    faster_whisper_custom_model_path=DEFAULT_MODEL)
        self.listener = serve(handler, '127.0.0.1', 0)
        self.addCleanup(self.listener.shutdown)
        self.worker = threading.Thread(target=self.listener.serve_forever, daemon=True)
        self.worker.start()
        self.url = 'ws://127.0.0.1:'+str(self.listener.socket.getsockname()[1])

    def control(self, operation='status', **fields):
        with connect(self.url) as ws:
            ws.send(json.dumps({'type':'asr_model_control','operation':operation,**fields}))
            return json.loads(ws.recv(timeout=3))

    def start(self):
        ws = connect(self.url)
        self.addCleanup(ws.close)
        ws.send(json.dumps({'uid':'broadcast', 'model':'small', 'language':'ca', 'task':'transcribe', 'use_vad':True}))
        reply=json.loads(ws.recv(timeout=3))
        if reply.get('status')=='LOADING':
            reply=json.loads(ws.recv(timeout=3))
        return ws,reply

    def wait_idle(self):
        deadline=time.monotonic()+4
        while self.server.model_selection.status()['active_sessions']:
            self.assertLess(time.monotonic(),deadline)
            time.sleep(.01)

    def test_release_prefix_profile_rejects_batch_before_loading(self):
        self.server.batch_config = {}
        with patch.dict('os.environ', {'WHISPER_PREFIX_MODELS':'small,systran-large,bsc-large'}):
            _, reply = self.start()
        self.assertEqual(reply['error_code'], 'unsupported_prefix_client')
        self.assertEqual(self.events, [])
        self.assertEqual(self.control()['state']['active_sessions'], 0)

    def test_control_does_not_allocate_and_pending_model_switch_drains_old_work(self):
        self.assertEqual(self.control()['state']['active_sessions'],0)
        self.assertEqual(self.events,[])
        first,ready=self.start()
        self.assertEqual(ready['message'],'SERVER_READY')
        self.assertEqual(ready['model'],DEFAULT_MODEL)  # Ignores legacy per-event small.
        selected=self.control('select',model=MODELS[2]['id'],expected_revision=0)
        self.assertTrue(selected['state']['pending'])
        _,blocked=self.start()
        self.assertEqual(blocked['error_code'],'model_busy')
        self.assertEqual(self.control()['state']['active_sessions'],1)
        self.drain.clear()
        first.send(b'END_OF_AUDIO')
        first.close()
        time.sleep(.05)
        self.assertEqual(self.control()['state']['active_sessions'],1)
        self.assertEqual(len(self.events),1)  # No premature unload during inference.
        self.drain.set()
        self.wait_idle()
        second,ready=self.start()
        self.assertEqual(ready['model'],MODELS[2]['id'])
        self.assertEqual(self.events,[('load',DEFAULT_MODEL),('unload',DEFAULT_MODEL),('load',MODELS[2]['id'])])
        second.close()
        self.wait_idle()

    def test_failed_load_never_registers_a_client_and_allows_retry(self):
        self.control('select',model=MODELS[1]['id'],expected_revision=0)
        with patch.object(ServeClientFasterWhisper,'create_model',side_effect=RuntimeError('private provider error')):
            _,failure=self.start()
        self.assertEqual(failure['error_code'],'model_load_failed')
        self.assertNotIn('private provider error',json.dumps(failure))
        self.assertEqual(self.server.client_manager.clients,{})
        self.assertEqual(self.control()['state']['active_sessions'],0)
        ws,ready=self.start()
        self.assertEqual(ready['model'],MODELS[1]['id'])
        ws.close()
        self.wait_idle()

    def test_inference_exception_releases_shared_lock(self):
        ws, ready = self.start()
        client = next(iter(self.server.client_manager.clients.values()))
        with patch.object(client.transcriber, 'transcribe', create=True,
                          side_effect=RuntimeError('inference failed')):
            with self.assertRaises(RuntimeError):
                client.transcribe_audio([])
        self.assertTrue(self.server.model_selection.inference_lock.acquire(blocking=False))
        self.server.model_selection.inference_lock.release()
        ws.close()
        self.wait_idle()

    def test_disabled_mode_rejects_control_without_loading_or_registering(self):
        self.server.model_selection = None
        reply = self.control('select', model=MODELS[1]['id'], expected_revision=0)
        self.assertEqual(reply['error'], 'selection_disabled')
        self.assertEqual(self.server.client_manager.clients, {})
        self.assertEqual(self.events, [])

    def test_incompatible_inference_modes_fail_before_startup(self):
        for options in [{'batch_enabled': True}, {'enable_rest': True}, {'backend': 'tensorrt'}]:
            server = TranscriptionServer()
            arguments = {'backend': 'faster_whisper', 'model_selection_state': '/unused/state.json'}
            arguments.update(options)
            with self.assertRaisesRegex(ValueError, 'Shared model selection requires'):
                server.run('127.0.0.1', **arguments)


if __name__ == '__main__':
    unittest.main()
