"""Lossless fallback when an immutable short prefix no longer matches."""
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import numpy as np
import pytest

from whisper_live.fast_prefix import FastTextPrefix, words
from whisper_live.stable_prefix import AlignedWord, PrefixContextLost
from whisper_live.backend.base import ServeClientBase
from whisper_live.backend.faster_whisper_backend import ServeClientFasterWhisper as Backend


def aligned(text, start=0., step=.2):
    return [AlignedWord(w, start+i*step, start+(i+1)*step)
            for i, w in enumerate(words(text))]


@pytest.mark.parametrize('old,new,shared', [
    ('We saw the', 'We saw another example today', 2),
    ('These books', 'This book contains several chapters', 0),
    ('It is about the', 'It is about applications of a new law', 3),
    ('Some old repeated words', 'Let us consider another possibility', 0),
    ('One', 'Another completely different explanation follows', 0),
])
def test_reopen_never_consumes_disputed_words_or_rewrites_history(old, new, shared):
    p = FastTextPrefix()
    p.confirmed = words(old)
    p.timed = aligned(old)
    p.previous, p.previous_end = words('old pending draft'), 2.
    p.sequence = 1
    p.history = [{'sequence': 1, 'text': old}]
    history = deepcopy(p.history)
    current = aligned(new)
    assert p.reopen_rewritten_context(current, 3.) is None
    assert p.reopen_rewritten_context(current, 3.) is None  # Same audio cannot agree.
    assert p.reopen_rewritten_context(current, 3.2) is None
    assert p.reopen_rewritten_context(current, 3.6) == shared
    assert p.confirmed == [w.text for w in current[:shared]]
    assert p.previous == [w.text for w in current[shared:]] and p.previous_end == 3.
    assert p.history == history and p.sequence == 1
    assert p.draft == ''.join(w.text for w in current[shared:]).strip()
    # Ordinary agreement uses the first NEW recovery snapshot, never the old
    # disputed hypothesis. Two fresh snapshots suffice; no third is required.
    committed = p.observe([w.text for w in current], 3.6)
    assert committed == [w.text for w in current[shared:-2]]
    assert p.history[0] == history[0]


def test_reopen_keeps_a_new_negation_and_a_word_crossing_the_old_audio_boundary():
    p = FastTextPrefix()
    p.confirmed = words('we accepted it')
    p.timed = aligned('we accepted it')
    current = aligned('we accepted no longer works')
    current[2] = AlignedWord(' no', .4, 1.5)
    current[3] = AlignedWord(' longer', 1.5, 2.)
    current[4] = AlignedWord(' works', 2., 2.5)
    assert p.reopen_rewritten_context(current, 3.) is None
    assert p.reopen_rewritten_context(current, 3.6) == 2
    assert 'no longer works' == p.draft


@pytest.mark.parametrize('case', ['too_many_words', 'too_long', 'missing_times',
                                  'nonfinite', 'no_new_suffix', 'unchanged'])
def test_unbounded_or_unvalidated_context_cannot_reopen(case):
    p = FastTextPrefix()
    p.confirmed, p.timed = words('old context'), aligned('old context')
    current, end = aligned('new words continue here'), 3.
    if case == 'too_many_words':
        p.confirmed = words('one two three four five six seven eight nine')
        p.timed = aligned(''.join(p.confirmed))
    elif case == 'too_long':
        p.timed = aligned('old context', step=3.)
    elif case == 'missing_times':
        p.timed = []
    elif case == 'nonfinite':
        end = float('nan')
    elif case == 'no_new_suffix':
        current = aligned('new')
    elif case == 'unchanged':
        current = aligned('old context continues here')
    for _ in range(3):
        assert p.reopen_rewritten_context(current, end) is None
        end += 1.


def test_changed_candidate_and_normal_observation_reset_recovery_evidence():
    p = FastTextPrefix()
    p.confirmed, p.timed = words('old context'), aligned('old context')
    assert p.reopen_rewritten_context(aligned('new words continue here'), 3.) is None
    assert p.reopen_rewritten_context(aligned('other words continue here'), 4.) is None
    assert p.reopen_rewritten_context(aligned('new words continue here'), 5.) is None
    p.observe(words('old context is still here'), 6.)
    assert p.reopen_rewritten_context(aligned('new words continue here'), 7.) is None


def test_backend_reopen_keeps_audio_offset_and_requires_fresh_confirmation():
    c = Backend.__new__(Backend)
    ServeClientBase.__init__(c, 'reopen-unit', MagicMock(), translation_queue=None,
                            max_uncommitted_audio_s=10, context_overlap_s=5)
    c.stable_prefix = p = FastTextPrefix()
    c._fast_last_commit = None
    c._fast_samples = np.zeros(9*16000, dtype=np.float32)
    c.use_vad = False
    p.confirmed, p.timed = words('old context'), aligned('old context')
    p.history = [{'sequence': 1, 'text': 'old context'}]
    p.sequence = 1
    c.frames_np = np.arange(12*16000, dtype=np.float32)
    before = c.frames_np.copy()
    c._fast_align = MagicMock(return_value=aligned('new words continue here'))
    result = [NS(text='new words continue here', no_speech_prob=.1, avg_logprob=-.2)]
    c.handle_transcription_output(result, 8.)
    assert p.confirmed == words('old context')
    c.handle_transcription_output(result, 8.6)
    assert p.confirmed == words('new words') and p.draft == 'continue here'
    assert len(p.history) == 2
    assert c.timestamp_offset == 0 and not c.exit
    np.testing.assert_array_equal(c.frames_np, before)
    c.handle_transcription_output(result, 9.2)
    assert p.history[-1]['text'] == 'new words'
    assert p.history[0]['text'] == 'old context'
    assert c.timestamp_offset == 0


def test_same_snapshot_reopening_cannot_bypass_the_hard_window_limit():
    c = Backend.__new__(Backend)
    ServeClientBase.__init__(c, 'limit-unit', MagicMock(), translation_queue=None,
                            max_uncommitted_audio_s=10, context_overlap_s=5)
    c.stable_prefix = FastTextPrefix()
    c.stable_prefix.confirmed = words('old context')
    c.stable_prefix.timed = aligned('old context')
    c._fast_last_commit = None
    c._fast_samples = np.zeros(20*16000, dtype=np.float32)
    c.use_vad = False
    c._fast_align = MagicMock(return_value=aligned('new words continue here'))
    with pytest.raises(PrefixContextLost):
        c.handle_transcription_output([NS(text='new words continue here', no_speech_prob=.1)], 20.)
    assert c.exit and c.timestamp_offset == 0


def acoustic_revision():
    p = FastTextPrefix()
    p.confirmed = words('the old description')
    p.timed = aligned('the old description', step=.4)
    p.sequence = 1
    p.history = [{'sequence': 1, 'text': 'the old description'}]
    current = aligned('a new description keeps pending speech', step=.4)
    return p, current


def test_acoustic_recovery_changes_working_words_without_republishing_or_retiring():
    p, current = acoustic_revision()
    history = deepcopy(p.history)
    assert p.recover_closed_audio(current, 3.) is None
    assert p.recover_closed_audio(current, 3.) is None
    assert p.recover_closed_audio(current, 3.2) is None
    assert p.recover_closed_audio(current, 3.6) == 3
    assert ''.join(p.confirmed).strip() == 'a new description'
    assert p.history == history and p.sequence == 1
    assert ''.join(p.observe([w.text for w in current], 3.6)).strip() == 'keeps'
    assert p.draft == 'pending speech'


def test_acoustic_recovery_never_consumes_a_word_crossing_the_published_frontier():
    p, current = acoustic_revision()
    p.timed = aligned('the old description', step=.3)
    # Last new word begins in old audio but extends into unconfirmed speech.
    current[2] = AlignedWord(' description', .8, 1.6)
    current[3] = AlignedWord(' keeps', 1.6, 2.)
    current[4] = AlignedWord(' pending', 2., 2.4)
    current[5] = AlignedWord(' speech', 2.4, 2.8)
    assert p.recover_closed_audio(current, 3.) is None
    assert p.recover_closed_audio(current, 4.) == 2
    assert ''.join(p.confirmed).strip() == 'a new'
    assert p.observe([w.text for w in current], 4.) == [current[2].text, current[3].text]
    # The crossing word was NOT consumed as old context: it is delivered only
    # by ordinary new-word agreement, using the two fresh hypotheses.
    assert p.history[-1]['text'].startswith('description')
    assert p.history[0] == {'sequence': 1, 'text': 'the old description'}


@pytest.mark.parametrize('case', ['later_repeat', 'zero_old', 'zero_new', 'nonfinite',
                                  'overlap', 'no_pending', 'different_words', 'drifting_boundary'])
def test_acoustic_recovery_declines_unsafe_or_unstable_evidence(case):
    p, current = acoustic_revision()
    if case == 'later_repeat':
        current = [AlignedWord(w.text, w.start+2, w.end+2) for w in current]
    elif case == 'zero_old':
        p.timed[1] = AlignedWord(' old', .4, .4)
    elif case == 'zero_new':
        current[0] = AlignedWord('a', 0., 0.)
        current[1] = AlignedWord(' new', .4, .4)
    elif case == 'nonfinite':
        current[1] = AlignedWord(' new', float('nan'), .8)
    elif case == 'overlap':
        current[3] = AlignedWord(' keeps', 1.1, 1.6)
    elif case == 'no_pending':
        current = current[:3]
    assert p.recover_closed_audio(current, 3.) is None
    if case == 'different_words':
        current[1] = AlignedWord(' other', .4, .8)
    elif case == 'drifting_boundary':
        current[2] = AlignedWord(' description', .8, 1.08)
    assert p.recover_closed_audio(current, 3.6) is None
    assert p.history == [{'sequence': 1, 'text': 'the old description'}]


def test_acoustic_recovery_drift_needs_fresh_evidence_at_the_new_boundary():
    p, current = acoustic_revision()
    assert p.recover_closed_audio(current, 3.) is None
    current[2] = AlignedWord(' description', .8, 1.08)
    assert p.recover_closed_audio(current, 3.6) is None
    assert p.recover_closed_audio(current, 3.8) is None
    assert p.recover_closed_audio(current, 4.2) == 3


def test_sparse_confirmed_context_can_advance_after_two_checked_alignments():
    p = FastTextPrefix()
    current = aligned('earlier confirmed words still pending', step=.8)
    p.confirmed = [w.text for w in current[:3]]
    p.timed = current[:3]
    p.previous = words('still pending')
    p.previous_end = 9.
    p.history = [{'sequence': 1, 'text': 'earlier confirmed words'}]
    history = deepcopy(p.history)
    assert p.checked_short_trim(current, 0., 10., 5.) is None
    assert p.checked_short_trim(current, 0., 10., 5.) is None
    assert p.checked_short_trim(current, 0., 10.2, 5.) is None
    cut = p.checked_short_trim(current, 0., 10.6, 5.)
    assert cut == pytest.approx(1.6)
    p.move_window(cut)
    assert ''.join(p.confirmed).strip() == 'words'
    assert p.previous == words('still pending') and p.previous_end == 9.
    assert p.history == history
    assert p.checked_short_trim(current, cut, 11.2, 5.) is None


@pytest.mark.parametrize('case', ['zero_duration', 'new_speech', 'short_overlap', 'boundary_drift'])
def test_sparse_trim_preserves_uncertain_speech(case):
    p = FastTextPrefix()
    current = aligned('earlier confirmed words still pending', step=.8)
    p.confirmed = [w.text for w in current[:2]]
    if case == 'zero_duration':
        current[1] = AlignedWord(' confirmed', .8, .8)
    elif case == 'new_speech':
        p.confirmed = words('earlier')
    elif case == 'short_overlap':
        current = aligned('earlier confirmed words still pending', start=9., step=.8)
    assert p.checked_short_trim(current, 0., 10., 5.) is None
    if case == 'boundary_drift':
        current[0] = AlignedWord('earlier', 0., 1.2)
        current[1] = AlignedWord(' confirmed', 1.2, 1.6)
    assert p.checked_short_trim(current, 0., 10.6, 5.) is None


def test_backend_checked_boundary_avoids_the_sparse_prefix_hard_stop_and_keeps_new_audio():
    c = Backend.__new__(Backend)
    ServeClientBase.__init__(c, 'sparse-window-unit', MagicMock(), translation_queue=None,
                            max_uncommitted_audio_s=10, context_overlap_s=5)
    c.stable_prefix = p = FastTextPrefix()
    c._fast_last_commit = None
    c._fast_samples = np.zeros(20*c.RATE, dtype=np.float32)
    c.frames_np = np.ones(23*c.RATE, dtype=np.float32)
    c.use_vad = False
    current = aligned('earlier confirmed words still pending', step=.8)
    p.confirmed = [w.text for w in current[:3]]
    p.timed = current[:3]
    p.sequence = 1
    p.history = [{'sequence': 1, 'text': 'earlier confirmed words'}]
    p.previous, p.previous_end = words('still pending'), 9.
    c._fast_align = MagicMock(return_value=current)
    result = [NS(text='earlier confirmed words still pending', no_speech_prob=.1, avg_logprob=-.2)]
    c.handle_transcription_output(result, 10.)
    assert c.timestamp_offset == 0.
    c.handle_transcription_output(result, 20.)
    assert not c.exit and c.timestamp_offset == pytest.approx(1.6)
    assert p.confirmed == words(' words')
    assert p.draft == 'still pending'
    assert p.history == [{'sequence': 1, 'text': 'earlier confirmed words'}]
    assert c.frames_np.size == 23*c.RATE
    assert len(c.get_audio_chunk_for_processing()[0])/c.RATE == pytest.approx(20.)
    c.websocket.close.assert_not_called()


def test_acoustic_evidence_is_invalidated_by_normal_agreement_or_a_window_move():
    p, current = acoustic_revision()
    assert p.recover_closed_audio(current, 3.) is None
    p.observe(words('the old description still stands'), 3.2)
    assert p.recover_closed_audio(current, 3.6) is None
    p.move_window(0.)
    assert p.recover_closed_audio(current, 4.2) is None
    assert p.recover_closed_audio(current, 4.8) == 3


def test_same_audio_with_changed_trim_positions_does_not_supply_fresh_evidence():
    p = FastTextPrefix()
    current = aligned('earlier confirmed words still pending', step=.8)
    p.confirmed = [w.text for w in current[:3]]
    assert p.checked_short_trim(current, 0., 10., 5.) is None
    current[1] = AlignedWord(' confirmed', .8, 1.8)
    current[2] = AlignedWord(' words', 1.8, 2.4)
    assert p.checked_short_trim(current, 0., 10.2, 5.) is None
    assert p.checked_short_trim(current, 0., 10.6, 5.) is None
    assert p.checked_short_trim(current, 0., 11.2, 5.) == pytest.approx(1.8)


def test_slow_prefix_inference_caps_the_snapshot_without_dropping_queued_audio():
    c = Backend.__new__(Backend)
    ServeClientBase.__init__(c, 'queued-window-unit', MagicMock(), translation_queue=None,
                            max_uncommitted_audio_s=10, context_overlap_s=5)
    c.stable_prefix = FastTextPrefix()
    c.frames_np = np.arange(31*c.RATE, dtype=np.float32)
    before = c.frames_np.copy()
    samples, duration = c.get_audio_chunk_for_processing()
    assert duration == 20.
    np.testing.assert_array_equal(samples, before[:20*c.RATE])
    np.testing.assert_array_equal(c.frames_np, before)
    assert c.timestamp_offset == 0.
    c.timestamp_offset = 15.
    samples, duration = c.get_audio_chunk_for_processing()
    assert duration == 16.
    np.testing.assert_array_equal(samples, before[15*c.RATE:])
    c.stable_prefix = None  # Native timestamp-policy snapshot handling is unchanged.
    c.timestamp_offset = 0.
    samples, duration = c.get_audio_chunk_for_processing()
    assert duration == 31.
    np.testing.assert_array_equal(samples, before)


def test_alignment_before_the_soft_limit_can_support_a_checked_cut_at_that_limit():
    c = Backend.__new__(Backend)
    ServeClientBase.__init__(c, 'early-evidence-unit', MagicMock(), translation_queue=None,
                            max_uncommitted_audio_s=10, context_overlap_s=5)
    c.stable_prefix = p = FastTextPrefix()
    c._fast_last_commit = None
    c._fast_samples = np.zeros(10*c.RATE, dtype=np.float32)
    c.use_vad = False
    current = aligned('earlier confirmed words still pending', step=.8)
    p.confirmed = [w.text for w in current[:3]]
    p.timed = current[:3]
    c._fast_align = MagicMock(return_value=current)
    c._fast_reconcile(current, 9.)
    assert c.timestamp_offset == 0.
    c.handle_transcription_output([NS(text='earlier confirmed words still pending',
                                     no_speech_prob=.1, avg_logprob=-.2)], 10.)
    assert c.timestamp_offset == pytest.approx(1.6)
    assert p.confirmed == words(' words') and p.draft == 'still pending'
    c.websocket.close.assert_not_called()


def test_recovery_accepts_zero_duration_decoration_only_with_real_neighbouring_audio():
    p, current = acoustic_revision()
    current[:3] = [AlignedWord('a', 0., 0.), AlignedWord(' new', 0., .4),
                   AlignedWord(' description', .4, 1.2)]
    assert p.recover_closed_audio(current, 3.) is None
    assert p.recover_closed_audio(current, 3.6) == 3
    assert p.history == [{'sequence': 1, 'text': 'the old description'}]


def test_acoustic_recovery_can_reuse_its_first_alignment_for_safe_trimming():
    p = FastTextPrefix()
    p.confirmed = words('the old description')
    p.timed = aligned('the old description', step=.8)
    current = aligned('a new description keeps pending speech', step=.8)
    assert p.recover_closed_audio(current, 10., offset=0., overlap_s=5.) is None
    assert p.recover_closed_audio(current, 10.6, offset=0., overlap_s=5.) == 3
    assert p.checked_short_trim(current, 0., 10.6, 5.) == pytest.approx(1.6)


def test_repeated_same_audio_cannot_walk_acoustic_boundary_evidence_forward():
    p, current = acoustic_revision()
    assert p.recover_closed_audio(current, 3.) is None
    current[2] = AlignedWord(' description', .8, 1.14)
    assert p.recover_closed_audio(current, 3.) is None
    current[2] = AlignedWord(' description', .8, 1.08)
    # Drift is measured against the FIRST alignment, not the immediately
    # preceding same-audio observation. Fresh evidence must start again.
    assert p.recover_closed_audio(current, 3.6) is None
    assert p.recover_closed_audio(current, 4.2) == 3


def test_reopen_uses_only_new_exact_word_agreement_with_punctuation():
    p = FastTextPrefix()
    p.confirmed, p.timed = words('old context'), aligned('old context')
    first = aligned('new words, continue here with more speech')
    second = aligned('new words continue here with more speech')
    assert p.reopen_rewritten_context(first, 3.) is None
    assert p.reopen_rewritten_context(second, 3.6) == 0
    # Recovery keys ignore punctuation to locate context; publication agreement
    # still sees the comma change and may not confirm past it.
    assert p.observe([w.text for w in second], 3.6) == []
    assert not p.history


def test_late_acoustic_recovery_can_confirm_fresh_pending_text_and_trim_on_second_snapshot():
    c = Backend.__new__(Backend)
    ServeClientBase.__init__(c, 'late-recovery-unit', MagicMock(), translation_queue=None,
                            max_uncommitted_audio_s=10, context_overlap_s=5)
    c.stable_prefix = p = FastTextPrefix()
    c.timestamp_offset, c.frames_offset = 44., 40.
    c.frames_np = np.ones(25*c.RATE, dtype=np.float32)
    c._fast_last_commit = None
    c.use_vad = False
    p.confirmed = words('previously closed altered wording')
    p.timed = [AlignedWord(w, a+44., b+44.) for w,a,b in zip(p.confirmed,
                [0., .54, .86, 1.94], [.54, .86, 1.94, 2.62])]
    p.sequence = 1
    p.history = [{'sequence':1,'text':'previously closed altered wording'}]
    text = 'the revised phrase has different internal wording new pending words remain here'
    timings = [(0.,0.),(0.,.08),(.08,.44),(.44,.7),(.7,1.),(1.,1.94),(1.94,2.62),
               (2.62,3.),(3.,3.4),(3.4,3.8),(3.8,4.2),(4.2,4.6)]
    current = [AlignedWord(w,a+44.,b+44.) for w,(a,b) in zip(words(text),timings)]
    c._fast_align = MagicMock(return_value=current)
    result = [NS(text=text,no_speech_prob=.1,avg_logprob=-.2)]
    c._fast_samples = np.zeros(19*c.RATE,dtype=np.float32)
    c.handle_transcription_output(result,19.)
    assert not c.exit and c.timestamp_offset==44.
    c._fast_samples = np.zeros(round(19.9*c.RATE),dtype=np.float32)
    c.handle_transcription_output(result,19.9)
    assert not c.exit and c.timestamp_offset>45.
    assert p.history[0]=={'sequence':1,'text':'previously closed altered wording'}
    assert p.history[-1]['text']=='new pending words'
    assert p.draft=='remain here'
    assert len(c.frames_np)==25*c.RATE
    c.websocket.close.assert_not_called()


def test_checked_alignment_of_retained_audio_survives_a_window_move():
    p = FastTextPrefix()
    current = aligned('one two three four five six seven eight nine ten', step=.5)
    p.confirmed = [w.text for w in current[:8]]
    p.timed = current[:8]
    assert p.checked_short_trim(current, 0., 10., 5.) is None
    p.move_window(2.)
    cropped = current[4:]
    assert p.confirmed == [w.text for w in current[4:8]]
    # The second, growing snapshot may already be at its safety limit after
    # a slow decode. Old alignment still describes this exact retained audio.
    assert p.checked_short_trim(cropped, 2., 22., 5.) == pytest.approx(3.5)


def test_cropping_does_not_turn_the_same_audio_into_an_independent_alignment():
    p = FastTextPrefix()
    current = aligned('one two three four five six seven eight nine ten', step=.5)
    p.confirmed = [w.text for w in current[:8]]
    p.timed = current[:8]
    assert p.checked_short_trim(current, 0., 10., 5.) is None
    p.move_window(2.)
    assert p.checked_short_trim(current[4:], 2., 10., 5.) is None
    assert p.checked_short_trim(current[4:], 2., 10.2, 5.) is None
    assert p.checked_short_trim(current[4:], 2., 10.6, 5.) == pytest.approx(3.5)


def test_a_valid_subsecond_cut_can_progress_at_the_limit_while_retaining_four_context_words():
    c = Backend.__new__(Backend)
    ServeClientBase.__init__(c, 'subsecond-progress-unit', MagicMock(), translation_queue=None,
                            max_uncommitted_audio_s=10, context_overlap_s=5)
    c.stable_prefix = p = FastTextPrefix()
    c._fast_last_commit = None
    c._fast_samples = np.zeros(20*c.RATE, dtype=np.float32)
    c.use_vad = False
    text = 'the earlier phrase still has useful context pending new words here'
    endpoints = [.04,.68,.86,1.40,1.56,1.86,2.78,3.2,3.5,4.,4.3]
    current = [AlignedWord(w,0. if i==0 else endpoints[i-1],endpoints[i])
               for i,w in enumerate(words(text))]
    p.confirmed = [w.text for w in current[:7]]
    p.timed = current[:7]
    p.sequence = 1
    p.history = [{'sequence':1,'text':''.join(p.confirmed).strip()}]
    c._fast_align = MagicMock(return_value=current)
    c.handle_transcription_output([NS(text=text,no_speech_prob=.1,avg_logprob=-.2)],20.)
    assert not c.exit and c.timestamp_offset==pytest.approx(.86)
    assert len(p.confirmed)==4 and p.draft=='pending new words here'
    assert len(p.history)==1
    c.websocket.close.assert_not_called()


def pause_client():
    c = Backend.__new__(Backend)
    ServeClientBase.__init__(c, 'pause-recovery-unit', MagicMock(), translation_queue=None,
                            max_uncommitted_audio_s=10, context_overlap_s=5)
    c.stable_prefix = p = FastTextPrefix()
    c._fast_last_commit = None
    c.use_vad, c.vad_parameters = True, {'threshold':.5}
    c._fast_samples = np.zeros(20*c.RATE,dtype=np.float32)
    c.frames_np = np.arange(25*c.RATE,dtype=np.float32)
    p.confirmed, p.timed = words('already shown'), aligned('already shown',step=.5)
    p.sequence = 1
    p.history = [{'sequence':1,'text':'already shown'}]
    text = 'already shown uncertain words complete this utterance'
    c.transcribe_audio = MagicMock(return_value=[NS(text=text,no_speech_prob=.1,avg_logprob=-.2)])
    c._fast_align = MagicMock(return_value=aligned(text,step=.5))
    return c


def test_verified_pause_closes_the_completed_utterance_explicitly_without_losing_later_audio():
    from unittest.mock import patch
    c = pause_client();before=c.frames_np.copy();snapshot=c._fast_samples
    speech=[{'start':0,'end':8*c.RATE},{'start':9*c.RATE,'end':19*c.RATE}]
    with patch('faster_whisper.vad.get_speech_timestamps',return_value=speech):
        assert c._recover_at_pause(20.)
    assert c.timestamp_offset==8.5 and not c.exit
    assert c.stable_prefix.history==[{'sequence':1,'text':'already shown'},
        {'sequence':2,'text':'uncertain words complete this utterance','finalization_reason':'vad_pause_recovery'}]
    assert not c.stable_prefix.confirmed
    assert len(c.transcribe_audio.call_args.args[0])/c.RATE==8.5
    assert c._fast_samples is snapshot
    np.testing.assert_array_equal(c.frames_np,before)
    c.websocket.close.assert_not_called()


@pytest.mark.parametrize('case',['no_pause','short_pause','pause_before_closed','unmatched_context',
                                  'rejected_final','invalid_alignment','decode_error'])
def test_pause_recovery_refuses_unsafe_or_unaccepted_final_hypotheses(case):
    from unittest.mock import patch
    c=pause_client();speech=[{'start':0,'end':8*c.RATE},{'start':9*c.RATE,'end':19*c.RATE}]
    if case=='no_pause':speech=[{'start':0,'end':20*c.RATE}]
    elif case=='short_pause':speech[1]['start']=round(8.3*c.RATE)
    elif case=='pause_before_closed':
        c.stable_prefix.timed=aligned('already shown',start=10.,step=.5)
    elif case=='unmatched_context':
        c._fast_align.return_value=aligned('different unrelated words here',step=.5)
        c.stable_prefix.confirmed=words('one two three four five six seven eight nine')
        c.stable_prefix.timed=aligned(''.join(c.stable_prefix.confirmed),step=.8)
    elif case=='rejected_final':c.transcribe_audio.return_value=[NS(text='wrong',no_speech_prob=.99,avg_logprob=-2)]
    elif case=='invalid_alignment':c._fast_align.side_effect=PrefixContextLost('invalid alignment')
    elif case=='decode_error':c.transcribe_audio.side_effect=RuntimeError('decode failed')
    before=c.frames_np.copy();snapshot=c._fast_samples
    with patch('faster_whisper.vad.get_speech_timestamps',return_value=speech):
        assert not c._recover_at_pause(20.)
        # A rejected identical endpoint is not decoded repeatedly.
        assert not c._recover_at_pause(20.)
    assert c.timestamp_offset==0. and c._fast_samples is snapshot
    assert c.stable_prefix.history==[{'sequence':1,'text':'already shown'}]
    np.testing.assert_array_equal(c.frames_np,before)


def test_pending_text_is_never_called_stable_agreement_when_closed_by_a_pause():
    p=FastTextPrefix()
    p.confirmed=words('already shown');p.history=[{'sequence':1,'text':'already shown'}];p.sequence=1
    new=p.close_utterance(words('already shown a best final hypothesis'))
    assert ''.join(new).strip()=='a best final hypothesis'
    assert p.packet()['confirmed'][-1]['finalization_reason']=='vad_pause_recovery'
    assert p.history[0]=={'sequence':1,'text':'already shown'}
    with pytest.raises(PrefixContextLost):p.close_utterance(words('unmatched correction must not overwrite'))


def test_pause_recovery_may_replay_a_bounded_correction_but_never_suppresses_unknown_words():
    from unittest.mock import patch
    c=pause_client();text='a revised description includes every pending word'
    c.transcribe_audio.return_value=[NS(text=text,no_speech_prob=.1,avg_logprob=-.2)]
    c._fast_align.return_value=aligned(text,step=.5)
    speech=[{'start':0,'end':8*c.RATE},{'start':9*c.RATE,'end':19*c.RATE}]
    with patch('faster_whisper.vad.get_speech_timestamps',return_value=speech):
        assert c._recover_at_pause(20.)
    assert c.stable_prefix.history[0]=={'sequence':1,'text':'already shown'}
    assert c.stable_prefix.history[-1]['text']==text
    assert c.stable_prefix.history[-1]['finalization_reason']=='vad_pause_recovery'
    assert c.timestamp_offset==8.5
