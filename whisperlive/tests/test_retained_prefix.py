"""Separate immutable confirmed text from retained recognition context."""
import queue
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import pytest

from whisper_live.backend.base import ServeClientBase
from whisper_live.backend.faster_whisper_backend import ServeClientFasterWhisper as Backend
from whisper_live.stable_prefix import AlignedWord, BSC_MODEL, PrefixContextLost, RetainedWordPrefix


def segment(text=' Una prova general que continua', step=.4):
    return NS(text=text, start=0, end=.02, no_speech_prob=.1,
              words=[NS(word=' '+word, start=i*step, end=(i+1)*step)
                     for i, word in enumerate(text.split())])


def client():
    value = Backend.__new__(Backend)
    ServeClientBase.__init__(value, 'retained-unit', MagicMock(), translation_queue=queue.Queue(),
                             max_uncommitted_audio_s=10, context_overlap_s=5)
    value.stable_prefix = RetainedWordPrefix()
    return value


def test_confirmation_keeps_audio_and_context_is_never_emitted_twice():
    c = client()
    c.update_segments([segment()], 3)
    draft = c.update_segments([segment()], 4)
    assert c.timestamp_offset == 0
    assert c.transcript[0]['text'] == ' Una prova general'
    assert draft['text'] == ' que continua'
    c.update_segments([segment()], 5)
    assert len(c.transcript) == 1
    c.update_segments([segment(' Una prova general que continua amb més paraules')], 6)
    assert [s['text'] for s in c.transcript] == [' Una prova general', ' que continua']
    assert c.translation_queue.qsize() == 2
    assert c.timestamp_offset == 0


def test_identical_audio_does_not_confirm_the_retained_tail():
    p = RetainedWordPrefix()
    for _ in range(10):
        assert p.observe([segment()], 0, 3, .45)[0] == []


def test_punctuation_change_in_closed_context_does_not_replay_it():
    p = RetainedWordPrefix()
    p.observe([segment()], 0, 3, .45)
    p.observe([segment()], 0, 4, .45)
    confirmed, _ = p.observe([segment(' Una prova general, que continua amb altres paraules')], 0, 5, .45)
    assert ''.join(w.text for w in confirmed) == ' que continua'


def test_punctuation_of_new_words_must_still_agree():
    p = RetainedWordPrefix()
    p.observe([segment(' Una prova general. que continua')], 0, 3, .45)
    confirmed, _ = p.observe([segment()], 0, 4, .45)
    assert ''.join(w.text for w in confirmed) == ' Una prova'


def test_corrected_closed_context_uses_unique_timed_suffix_without_rewriting_history():
    p = RetainedWordPrefix()
    original = segment(' Una prova general que continua amb més paraules ara després')
    p.observe([original], 0, 5, .45)
    confirmed, _ = p.observe([original], 0, 6, .45)
    assert len(confirmed) == 8
    corrected = segment(' La prova general que continua amb més paraules ara després de tot això')
    confirmed, _ = p.observe([corrected], 0, 7, .45)
    assert ''.join(w.text for w in confirmed) == ' ara després'


def test_ambiguous_anchor_blocks_confirmation():
    p = RetainedWordPrefix()
    p.confirmed = [AlignedWord(' a', 0, .1), AlignedWord(' b', .1, .2), AlignedWord(' c', .2, .3)]
    p.committed_end = .3
    assert p.observe([segment(' x a b c a b c nova cua', step=.1)], 0, 2, .45) is None
    assert p.blocked_reason == 'confirmed_context_mismatch'


def test_same_words_in_later_audio_are_not_discarded():
    p = RetainedWordPrefix()
    p.observe([segment()], 0, 3, .45)
    p.observe([segment()], 0, 4, .45)
    p.move_window(4)
    assert p.observe([segment()], 4, 3, .45)[0] == []
    confirmed, _ = p.observe([segment()], 4, 4, .45)
    assert ''.join(w.text for w in confirmed) == ' Una prova general'
    assert confirmed[0].start == 4


def test_trim_retains_confirmed_context_and_reanchors_the_next_window():
    p = RetainedWordPrefix()
    item = segment(' un dos tres quatre cinc sis set vuit nou deu')
    p.observe([item], 0, 5, .45)
    p.observe([item], 0, 6, .45)
    assert p.trim_point(4, 2) == pytest.approx(2)
    p.move_window(2)
    assert [w.text.strip() for w in p.confirmed] == ['sis', 'set', 'vuit']
    after = segment(' sis set vuit nou deu onze dotze tretze')
    assert not p.observe([after], 2, 4, .45)[0]
    confirmed, _ = p.observe([after], 2, 5, .45)
    assert ''.join(w.text for w in confirmed) == ' nou deu onze'


def test_backend_trims_at_limit_without_replaying_confirmed_text():
    c = client()
    item = segment(' un dos tres quatre cinc sis set vuit nou deu onze dotze tretze catorze quinze')
    c.update_segments([item], 7)
    c.update_segments([item], 8)
    closed_count = len(c.transcript)
    c.update_segments([item], 10)
    assert len(c.transcript) == closed_count
    assert 0 < c.timestamp_offset <= 5
    assert c.timestamp_offset == pytest.approx(4.8)
    assert [w.text.strip() for w in c.stable_prefix.confirmed] == ['tretze']


def test_bounded_fallback_contains_only_unconfirmed_tail():
    c = client()
    c.update_segments([segment()], 3)
    c.update_segments([segment()], 4)
    c.update_segments([segment()], 11)
    assert c.transcript[-1]['finalization_reason'] == 'bounded_window'
    assert c.transcript[-1]['text'] == 'que continua'
    assert c.timestamp_offset == 6


def test_new_stable_chunk_can_revise_prior_provisional_times():
    c = client()
    c.update_segments([segment()], 3)
    c.update_segments([segment()], 4)
    c.transcript.append(c.format_segment(1.2, 10, 'que continua', completed=True, finalization_reason='bounded_window'))
    c.update_segments([segment(' Una prova general que continua amb més text')], 5)
    assert c.transcript[-1]['text'] == ' que continua'
    assert float(c.transcript[-1]['start']) < float(c.transcript[-1]['end']) < 10


def test_unsafe_hypothesis_waits_without_replaying_confirmed_text():
    c = client()
    c.update_segments([segment()], 3)
    draft = c.update_segments([segment()], 4)
    bad = segment()
    bad.words = None
    assert c.update_segments([bad], 5) == draft
    assert len(c.transcript) == 1
    assert c.timestamp_offset == 0


def test_lost_context_at_limit_explicitly_aborts_instead_of_dropping_audio():
    c = client()
    c.update_segments([segment()], 3)
    c.update_segments([segment()], 4)
    bad = segment()
    bad.words = None
    with pytest.raises(PrefixContextLost):
        c.update_segments([bad], 11)
    assert len(c.transcript) == 1 and c.timestamp_offset == 0
    assert c.exit
    c.websocket.close.assert_called_once()


def test_invalid_alignment_before_first_confirmation_uses_provisional_safety_path():
    c = client()
    bad = segment()
    bad.words = None
    c.update_segments([bad], 10)
    assert c.transcript[0]['finalization_reason'] == 'bounded_window'
    assert c.timestamp_offset == 5


def test_retained_mode_is_explicitly_opt_in():
    with patch.dict('os.environ', {'WHISPER_BSC_STABLE_PREFIX': 'retain_context'}), \
            patch.object(Backend, 'create_model'), patch('torch.cuda.is_available', return_value=False), \
            patch('threading.Thread'):
        c = Backend(MagicMock(), model=BSC_MODEL, language='ca')
    assert isinstance(c.stable_prefix, RetainedWordPrefix)


def test_rejected_tail_preserves_agreement_on_valid_speech():
    p = RetainedWordPrefix()
    p.observe([segment()], 0, 3, .45)
    rejected = segment(' hallucinated tail')
    rejected.no_speech_prob = .9
    confirmed, remaining = p.observe([segment(), rejected], 0, 4, .45)
    assert ''.join(w.text for w in confirmed) == ' Una prova general'
    assert ''.join(w.text for w in remaining) == ' que continua'


def test_rejected_middle_cannot_authorize_trimming_past_the_gap():
    p = RetainedWordPrefix()
    first = segment()
    rejected = segment(' uncertain middle')
    rejected.no_speech_prob = .9
    later = segment(' recognizable later words')
    p.observe([first, rejected, later], 0, 3, .45)
    confirmed, remaining = p.observe([first, rejected, later], 0, 4, .45)
    assert ''.join(w.text for w in confirmed + remaining) == first.text
    assert all(w.end <= first.words[-1].end for w in p.confirmed)


def test_rejected_hypothesis_can_trim_already_confirmed_audio_only():
    c = client()
    item = segment(' un dos tres quatre cinc sis set vuit nou deu onze dotze tretze catorze quinze')
    c.update_segments([item], 7)
    c.update_segments([item], 8)
    count = len(c.transcript)
    rejected = segment(' unrecognizable new hypothesis')
    rejected.no_speech_prob = .9
    c.update_segments([rejected], 10)
    assert c.timestamp_offset == pytest.approx(4.8)
    assert len(c.transcript) == count
    assert not c.exit


def test_historical_trim_after_window_move_does_not_require_new_alignment():
    p = RetainedWordPrefix()
    item = segment(' un dos tres quatre cinc sis set vuit nou deu')
    p.observe([item], 0, 5, .45)
    p.observe([item], 0, 6, .45)
    p.move_window(1)
    assert p.latest_words == []
    assert p.trim_point(8, 5) == pytest.approx(2.8)


def test_short_retained_suffix_can_match_only_at_the_same_audio_position():
    p = RetainedWordPrefix()
    p.confirmed = [AlignedWord(' clients', 0, .4), AlignedWord(' tenim', .4, .9), AlignedWord(' molts', .9, 1.4)]
    p.committed_end = 1.4
    item = segment(' tenim molts de molta gent', step=.7)
    assert p.observe([item], 0, 4, .45) is not None
    assert [w.text for w in p.confirmed] == [' tenim', ' molts']
    later = segment(' tenim molts de molta gent', step=.7)
    for word in later.words:
        word.start += 2
        word.end += 2
    assert p.observe([later], 0, 7, .45) is None


def test_one_retained_word_uses_alignment_to_skip_only_preceding_context():
    p = RetainedWordPrefix()
    p.confirmed = [AlignedWord(' qüestió', .1, .8)]
    p.committed_end = .8
    item = segment(' la qüestió que continua amb més text', step=.4)
    confirmed, remaining = p.observe([item], 0, 4, .45)
    assert not confirmed
    assert ''.join(w.text for w in remaining) == ' que continua amb més text'


@pytest.mark.parametrize('joint,logprob,accepted', [(False, -.3, False), (True, -.3, True),
                                                 (True, -1.1, False), (True, None, False)])
def test_joint_gate_is_opt_in_and_does_not_accept_low_confidence_silence(joint, logprob, accepted):
    p = RetainedWordPrefix(joint_speech_gate=joint)
    item = segment()
    item.no_speech_prob = .7
    item.avg_logprob = logprob
    decision = p.observe([item], 0, 3, .45)
    assert (decision is not None) is accepted


def test_joint_gate_requires_a_separate_explicit_mode():
    with patch.dict('os.environ', {'WHISPER_BSC_STABLE_PREFIX': 'retain_context_joint_gate'}), \
            patch.object(Backend, 'create_model'), patch('torch.cuda.is_available', return_value=False), \
            patch('threading.Thread'):
        c = Backend(MagicMock(), model=BSC_MODEL, language='ca')
    assert c.stable_prefix.joint_speech_gate


@pytest.mark.parametrize('text', [' no més paraules', ' bé no més paraules'])
def test_rapid_new_repetition_cannot_be_suppressed_as_old_context(text):
    p = RetainedWordPrefix()
    p.confirmed = [AlignedWord(' no', 0, .4)]
    p.committed_end = .4
    item = segment(text)
    if text.startswith(' no'):
        for word in item.words:
            word.start += .4
            word.end += .4
    assert p.observe([item], 0, 3, .45) is None
