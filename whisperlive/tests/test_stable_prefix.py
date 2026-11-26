"""Guard against committing changing text or trimming with invalid model times."""
import queue
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import pytest

from whisper_live.stable_prefix import StableWordPrefix
from whisper_live.backend.base import ServeClientBase


def segment(text=" Una prova general que continua", end=0.02):
    words = [NS(word=" " + word, start=i * 0.4, end=(i + 1) * 0.4)
             for i, word in enumerate(text.split())]
    return NS(text=text, words=words, start=0, end=end, no_speech_prob=0.1)


def test_commit_agreeing_prefix_and_keep_two_word_draft():
    policy = StableWordPrefix()
    first = segment()
    assert not policy.observe([first], 0, 2.5, .45)[0]
    confirmed, draft = policy.observe([first], 0, 3, .45)
    assert "".join(w.text for w in confirmed) == " Una prova general"
    assert "".join(w.text for w in draft) == " que continua"
    assert confirmed[-1].end == pytest.approx(1.2)  # Not native 0.02 s.


def test_same_audio_cannot_confirm_itself():
    policy = StableWordPrefix()
    for _ in range(20):
        assert not policy.observe([segment()], 0, 3, .45)[0]


def test_changed_word_stops_agreement():
    policy = StableWordPrefix()
    policy.observe([segment(" Una prova truncada que continua")], 0, 3, .45)
    confirmed, _ = policy.observe([segment(" Una prova corregida que continua")], 0, 4, .45)
    assert "".join(w.text for w in confirmed) == " Una prova"


def test_punctuation_is_not_added_or_normalized():
    policy = StableWordPrefix()
    policy.observe([segment(" Continua en solitari. després de molts anys")], 0, 4, .45)
    confirmed, _ = policy.observe([segment(" Continua en solitari després de molts anys")], 0, 5, .45)
    assert "".join(w.text for w in confirmed) == " Continua en"


def test_agreement_survives_audio_trim_without_repeating_committed_prefix():
    policy = StableWordPrefix()
    policy.observe([segment()], 0, 3, .45)
    confirmed, _ = policy.observe([segment()], 0, 4, .45)
    offset = confirmed[-1].end
    confirmed, _ = policy.observe([segment(" que continua amb altres paraules")], offset, 4, .45)
    assert "".join(w.text for w in confirmed) == " que continua"


def test_external_safety_reset_discards_old_agreement():
    policy = StableWordPrefix()
    policy.observe([segment()], 0, 3, .45)
    assert not policy.observe([segment()], 5, 3, .45)[0]


@pytest.mark.parametrize("defect", ["missing", "text_loss", "negative", "nan", "reversed", "future", "overlap", "no_speech"])
def test_unsafe_alignment_never_advances_audio(defect):
    policy = StableWordPrefix()
    item = segment()
    if defect == "missing": item.words = None
    if defect == "text_loss": item.text += " LOST"
    if defect == "negative": item.words[0].start = -1
    if defect == "nan": item.words[0].end = float('nan')
    if defect == "reversed": item.words[0].end = -.1
    if defect == "future": item.words[-1].end = 100
    if defect == "overlap": item.words[1].start = 0
    if defect == "no_speech": item.no_speech_prob = .99
    assert policy.observe([item], 0, 3, .45) is None


def test_recent_audio_is_kept_provisional():
    policy = StableWordPrefix(hold_words=0)
    policy.observe([segment()], 0, 2, .45)
    confirmed, _ = policy.observe([segment()], 0, 2.3, .45)
    assert confirmed[-1].end <= 1.8


def test_aligner_pieces_of_contractions_are_never_committed_separately():
    policy = StableWordPrefix(hold_words=1)
    item = segment(" ja s'intuïa la resta")
    item.words = [NS(word=' ja', start=0, end=.2), NS(word=' s', start=.2, end=.3),
                  NS(word="'intuïa", start=.3, end=.7), NS(word=' la', start=.7, end=.9),
                  NS(word=' resta', start=.9, end=1.2)]
    policy.observe([item], 0, 1.5, .45)
    confirmed, _ = policy.observe([item], 0, 2, .45)
    assert [w.text for w in confirmed] == [' ja', " s'intuïa", ' la']


def test_zero_length_tail_allows_only_the_safe_prefix():
    policy = StableWordPrefix(hold_words=0, hold_audio_s=0)
    item = segment()
    item.words[2].end = item.words[2].start
    policy.observe([item], 0, 3, .45)
    confirmed, _ = policy.observe([item], 0, 4, .45)
    assert ''.join(w.text for w in confirmed) == ' Una prova'


def test_backend_emits_translatable_closed_prefix_and_separate_draft():
    from whisper_live.backend.faster_whisper_backend import ServeClientFasterWhisper
    client = ServeClientFasterWhisper.__new__(ServeClientFasterWhisper)
    ServeClientBase.__init__(client, 'unit', MagicMock(), translation_queue=queue.Queue(),
                             max_uncommitted_audio_s=10, context_overlap_s=5)
    client.stable_prefix = StableWordPrefix()
    client.update_segments([segment()], 3)
    draft = client.update_segments([segment()], 4)
    final = client.translation_queue.get_nowait()
    assert final == client.transcript[0]
    assert final['completed'] and final['finalization_reason'] == 'stable'
    assert final['text'] == ' Una prova general'
    assert not draft['completed'] and draft['text'] == ' que continua'
    assert float(draft['start']) >= float(final['end'])
    assert client.timestamp_offset == pytest.approx(1.2)


def test_default_backend_policy_is_unchanged():
    from whisper_live.backend.faster_whisper_backend import ServeClientFasterWhisper
    client = ServeClientFasterWhisper.__new__(ServeClientFasterWhisper)
    ServeClientBase.__init__(client, 'unit', MagicMock())
    client.stable_prefix = None
    draft = client.update_segments([segment()], 3)
    assert not client.transcript and not draft['completed']
    assert client.timestamp_offset == 0


@pytest.mark.parametrize('model,flag,enabled', [
    ('BSC-LT/faster-whisper-large-v3-ca-punctuated-3370h', '0', False),
    ('BSC-LT/faster-whisper-large-v3-ca-punctuated-3370h', '1', True),
    ('Systran/faster-whisper-small', '1', False),
    ('Systran/faster-whisper-large-v3', '1', False),
])
def test_research_switch_is_opt_in_and_exact_model_scoped(model, flag, enabled):
    from whisper_live.backend.faster_whisper_backend import ServeClientFasterWhisper as Backend
    with patch.dict('os.environ', {'WHISPER_BSC_STABLE_PREFIX': flag}), \
            patch.object(Backend, 'create_model'), \
            patch('torch.cuda.is_available', return_value=False), \
            patch('threading.Thread'):
        client = Backend(MagicMock(), model=model, language='ca')
    assert (client.stable_prefix is not None) is enabled


def test_unstable_prefix_still_obeys_existing_bounded_window():
    from whisper_live.backend.faster_whisper_backend import ServeClientFasterWhisper as Backend
    client = Backend.__new__(Backend)
    ServeClientBase.__init__(client, 'unit', MagicMock(), max_uncommitted_audio_s=10, context_overlap_s=5)
    client.stable_prefix = StableWordPrefix()
    assert client.update_segments([segment()], 10) is None
    assert client.timestamp_offset == 5
    assert client.transcript[-1]['finalization_reason'] == 'bounded_window'
    assert not client.stable_prefix.previous


def test_tiny_stable_prefix_cannot_defeat_the_hard_window_limit():
    from whisper_live.backend.faster_whisper_backend import ServeClientFasterWhisper as Backend
    client = Backend.__new__(Backend)
    ServeClientBase.__init__(client, 'unit', MagicMock(), max_uncommitted_audio_s=10, context_overlap_s=5)
    client.stable_prefix = StableWordPrefix()
    client.update_segments([segment()], 9)
    assert client.update_segments([segment()], 10) is None
    assert client.timestamp_offset == 5
    assert client.transcript[-1]['finalization_reason'] == 'bounded_window'


def test_decoder_requests_alignment_only_for_experimental_policy():
    from whisper_live.backend.faster_whisper_backend import ServeClientFasterWhisper as Backend
    client = Backend.__new__(Backend)
    client.transcriber = MagicMock()
    client.transcriber.transcribe.return_value = ([], None)
    client.model_selection = None
    client.initial_prompt = None
    client.language = 'ca'
    client.task = 'transcribe'
    client.use_vad = True
    client.vad_parameters = {'threshold': .5}
    for enabled in (False, True):
        client.stable_prefix = StableWordPrefix() if enabled else None
        client.transcribe_audio([])
        kwargs = client.transcriber.transcribe.call_args.kwargs
        assert kwargs.get('word_timestamps', False) is enabled
