"""The offline replay must not turn decoder failures into skipped audio."""
import importlib.util
from pathlib import Path

import numpy as np


def runner():
    source = Path(__file__).resolve().parents[1] / 'scripts/evaluate_stable_prefix.py'
    spec = importlib.util.spec_from_file_location('prefix_replay', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_transient_decode_failure_retries_the_same_audio_offset():
    class Model:
        calls = 0
        def transcribe(self, audio, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise IndexError('alignment fixture')
            return None, None
    result = runner().replay(Model(), np.zeros(4 * 16000, dtype=np.float32), 'baseline')
    assert result['status'] == 'completed'
    assert len(result['inference_errors']) == 1
    events = result['events']
    failed = next(i for i, event in enumerate(events) if event['decode_error'])
    assert events[failed]['offset_before_s'] == events[failed]['offset_after_s']
    assert events[failed + 1]['offset_before_s'] == events[failed]['offset_before_s']


def test_repeated_decode_failures_are_an_explicit_aborted_trial():
    class Model:
        def transcribe(self, audio, **kwargs):
            raise IndexError('alignment fixture')
    result = runner().replay(Model(), np.zeros(4 * 16000, dtype=np.float32), 'baseline')
    assert result['status'] == 'aborted'
    assert len(result['inference_errors']) == 3
    assert all(e['offset_before_s'] == e['offset_after_s'] == 0 for e in result['events'])
