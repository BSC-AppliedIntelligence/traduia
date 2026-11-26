import asyncio

import pytest

from asr.whisper_client import WhisperLiveClient, TranscriptionChunk
from asr.caption_revisions import SourceCaptionRevisions


def message(seq, confirmed=(), after=0, draft=''):
    return {'text_prefix': {'version': 1, 'sequence': seq,
        'confirmed': [{'sequence': n, 'text': t} for n, t in confirmed],
        'after_confirmed': after, 'draft': draft}}


def consume(packets, error=None):
    async def run():
        c = WhisperLiveClient('localhost', 1)
        c._server_error = error
        for packet in packets[1:]:
            c._receive_queue.put_nowait(packet)
        c._receive_queue.put_nowait(None)
        return [v async for v in c._receive_text_prefix_transcriptions(packets[0])]
    return asyncio.run(run())


def test_replayed_ids_deduplicate_and_real_repetitions_survive():
    packets = [message(1, [(1, 'una prova')], 1, 'cua'),
               message(2, [(1, 'una prova'), (2, 'una prova')], 2)]
    result = consume(packets)
    assert [c.text for c in result if not c.is_partial] == ['una prova', 'una prova']
    assert result[0].chunk_id != result[-1].chunk_id
    assert all(c.prefix_protocol and c.latency_ms is None for c in result)
    assert result[-1].finalization_reason == 'stable_prefix'


def test_stale_packets_do_not_revert_draft_or_republish():
    packets = [message(2, [(1, 'hola')], 1, 'nova cua'), message(1, draft='vella cua')]
    assert [c.text for c in consume(packets)] == ['hola', 'nova cua', 'nova cua']


def test_missing_id_fails_before_publication():
    with pytest.raises(ValueError, match='gap'):
        consume([message(1, [(2, 'missing first')], 2)])


def test_closed_text_cannot_be_revised():
    with pytest.raises(ValueError, match='revised'):
        consume([message(1, [(1, 'hola')], 1), message(2, [(1, 'canvi')], 1)])


def test_watermark_must_match_confirmed_history():
    with pytest.raises(ValueError, match='watermark'):
        consume([message(1, [(1, 'hola')], 2, 'cua')])


def test_eof_flushes_only_remaining_draft_once():
    result = consume([message(1, [(1, 'prefix')], 1, 'cua')])
    assert [(c.text, c.is_partial, c.finalization_reason) for c in result] == [
        ('prefix', False, 'stable_prefix'), ('cua', True, 'stable_prefix'), ('cua', False, 'eos')]


def test_server_error_does_not_confirm_outstanding_draft():
    with pytest.raises(ConnectionError):
        consume([message(1, draft='cua')], error='unsafe retained context')


def test_untimed_prefixes_bypass_acoustic_revision_heuristics():
    revisions = SourceCaptionRevisions()
    for partial in [True, False]:
        c = TranscriptionChunk('text', is_partial=partial, prefix_protocol=True)
        assert revisions.receive(c) is c
