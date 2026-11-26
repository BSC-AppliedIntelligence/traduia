from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import pytest
import numpy as np

from whisper_live.fast_prefix import FastTextPrefix, accepted_text, words, align_text
from whisper_live.stable_prefix import AlignedWord, PrefixContextLost
from whisper_live.backend.base import ServeClientBase
from whisper_live.backend.faster_whisper_backend import ServeClientFasterWhisper as Backend, configured_fast_prefix, experimental_fast_prefix
from whisper_live.stable_prefix import BSC_MODEL
from whisper_live.stable_prefix import RetainedWordPrefix
from whisper_live.model_selection import ModelSelectionError


def test_growing_snapshots_confirm_prefix_without_acoustic_times():
    p = FastTextPrefix()
    assert p.observe(words(' Una prova general que continua'), 2) == []
    assert p.observe(words(' Una prova general que continua'), 2.2) == []
    assert ''.join(p.observe(words(' Una prova general que continua'), 2.6)) == ' Una prova general'
    assert p.packet()['confirmed'] == [{'sequence': 1, 'text': 'Una prova general'}]
    assert p.draft == 'que continua'


def test_tail_only_closes_on_agreement_and_vad_pause():
    p = FastTextPrefix()
    p.observe(words(' una prova'), 2, quiet=True)
    assert not p.history
    p.observe(words(' una prova'), 3, quiet=True)
    assert p.history[0]['text'] == 'una prova'
    assert p.draft == ''


def test_punctuation_is_never_fabricated_or_ignored_for_new_agreement():
    p = FastTextPrefix()
    p.observe(words(' una prova. general que continua'), 2)
    p.observe(words(' una prova general que continua'), 3)
    assert not p.history  # A lone short first token needs one more observation.
    p.observe(words(' una prova general que continua'), 3.6)
    assert p.history[0]['text'] == 'una prova general'


def test_isolated_short_first_word_needs_three_growing_observations():
    p=FastTextPrefix()
    assert p.observe(words('Sí.'),2,quiet=True)==[]
    assert p.observe(words('Sí.'),2.6,quiet=True)==[]
    assert p.draft=='Sí.'
    assert p.observe(words('Sí.'),2.8,quiet=True)==[]
    assert p.observe(words('Sí.'),3.2,quiet=True)==words('Sí.')


def test_short_initial_fragment_can_change_without_poisoning_closed_context():
    p=FastTextPrefix()
    p.observe(words('A...'),2,quiet=True)
    p.observe(words('A...'),2.6,quiet=True)
    assert not p.history and p.draft=='A...'
    p.observe(words('Després un altre llibre'),3.2)
    p.observe(words('Després un altre llibre'),3.8)
    assert p.history==[{'sequence':1,'text':'Després un'}]


@pytest.mark.parametrize('text',['A...','Doncs\u2026'])
def test_isolated_opening_ellipsis_stays_visible_but_unconfirmed(text):
    p=FastTextPrefix()
    for end in [2.,2.6,3.2,3.8]:
        assert p.observe(words(text),end,quiet=True)==[]
    assert not p.history and p.draft==text


def test_normal_first_word_and_multiword_phrase_keep_existing_agreement():
    for text in ['Hola,','Sí, és cert']:
        p=FastTextPrefix()
        p.observe(words(text),2,quiet=True)
        assert p.observe(words(text),2.6,quiet=True)==words(text)


def test_new_identical_spoken_phrase_gets_new_id():
    p = FastTextPrefix()
    p.observe(words(' una prova'), 2)
    p.observe(words(' una prova'), 3, quiet=True)
    p.observe(words(' una prova una prova'), 4)
    p.observe(words(' una prova una prova'), 5, quiet=True)
    assert p.history == [{'sequence': 1, 'text': 'una prova'}, {'sequence': 2, 'text': 'una prova'}]


def test_rejected_middle_cannot_discard_valid_prefix_or_skip_gap():
    segments = [NS(text='Una prova', no_speech_prob=.7, avg_logprob=-.2),
                NS(text='rebutjada', no_speech_prob=.8, avg_logprob=-2),
                NS(text='més tard', no_speech_prob=.1, avg_logprob=-.2)]
    assert ''.join(accepted_text(segments, .45)) == 'Una prova'


def test_recovery_requires_temporal_overlap_to_suppress_context():
    p = FastTextPrefix()
    p.confirmed = [' una', ' prova']
    p.timed = [AlignedWord(' una', 0, .4), AlignedWord(' prova', .4, .8)]
    assert p.align_context([AlignedWord(' nova', 1, 1.4), AlignedWord(' prova', 1.4, 1.8)]) is None
    assert p.confirmed == [' una', ' prova']


def short_correction(anchor='estat', before='fa', after='fine'):
    old=[AlignedWord(' '+w,a,b) for w,a,b in [
        (anchor,0,.32),(before,.32,.5),('intunejat',.5,1.24),
        ('a',1.24,1.5),('partir',1.5,1.86),('de',1.86,2.1),('un',2.1,2.36)]]
    current=[AlignedWord(' '+w,a,b) for w,a,b in [
        (anchor,0,.32),(after,.32,.54),('tunejat',.54,1.24),
        ('a',1.24,1.5),('partir',1.5,1.86),("d'un",1.86,2.4),('model',2.4,2.98)]]
    p=FastTextPrefix();p.confirmed=[w.text for w in old[:2]]
    p.timed=old[:2];p.history=[{'sequence':1,'text':anchor+' '+before}]
    p.sequence=1
    return p,old,current


@pytest.mark.parametrize('anchor,before,after',[
    ('estat','fa','fine'),('mostra','diu','nova'),('resultat','era','nou')])
def test_short_correction_requires_anchors_both_sides_and_fresh_agreement(anchor,before,after):
    p,old,current=short_correction(anchor,before,after)
    history=list(p.history)
    assert p.align_context(current) is None  # Existing lexical-only recovery declines.
    assert p.recover_short_context(old,current,3.) is None
    assert p.recover_short_context(old,current,3.2) is None
    assert p.recover_short_context(old,current,3.6)==2
    assert p.confirmed==[w.text for w in current[:2]]
    assert p.history==history  # Correct retained context, never republish closed text.
    assert current[2].text==' tunejat'  # Unconfirmed neighbouring speech is untouched.


def test_short_correction_same_snapshot_cannot_confirm_recovery():
    p,old,current=short_correction()
    for _ in range(4):assert p.recover_short_context(old,current,3.) is None
    assert p.confirmed==[w.text for w in old[:2]]


@pytest.mark.parametrize('case',[
    'no_right_anchor','later_right_anchor','later_phrase','repeated_anchor',
    'crosses_closed_boundary','new_leading_speech','zero_old_word','repeated_right_anchor',
])
def test_short_correction_declines_ambiguous_or_new_speech(case):
    p,old,current=short_correction()
    if case=='no_right_anchor':current[4]=AlignedWord(' altre',1.5,1.86)
    elif case=='later_right_anchor':current[4]=AlignedWord(' partir',2.5,2.86)
    elif case=='later_phrase':current=[AlignedWord(w.text,w.start+1,w.end+1) for w in current]
    elif case=='repeated_anchor':current.append(AlignedWord(' estat',3,3.5))
    elif case=='crosses_closed_boundary':current[1]=AlignedWord(' fine',.32,.8)
    elif case=='new_leading_speech':current.insert(0,AlignedWord(' no',0,.3))
    elif case=='zero_old_word':old[1]=AlignedWord(' fa',.5,.5)
    elif case=='repeated_right_anchor':current.append(AlignedWord(' partir',3,3.5))
    assert p.recover_short_context(old,current,3.) is None
    assert p.recover_short_context(old,current,4.) is None
    assert p.history==[{'sequence':1,'text':'estat fa'}]


def test_short_correction_boundary_drift_restarts_independent_evidence():
    p,old,current=short_correction()
    current[1]=AlignedWord(' fine',.32,.45)
    assert p.recover_short_context(old,current,3.) is None
    current[1]=AlignedWord(' fine',.32,.55)
    current[2]=AlignedWord(' tunejat',.55,1.24)
    assert p.recover_short_context(old,current,3.6) is None
    assert p.recover_short_context(old,current,4.2)==2


def test_short_correction_normal_agreement_invalidates_pending_recovery():
    p,old,current=short_correction()
    assert p.recover_short_context(old,current,3.) is None
    p.observe([w.text for w in old],3.6)
    assert p.recover_short_context(old,current,4.2) is None
    assert p.recover_short_context(old,current,4.8)==2


def test_short_correction_keeps_new_negation_and_resets_after_a_window_move():
    p,old,current=short_correction()
    current.extend([AlignedWord(' no',3,3.2),AlignedWord(' funciona',3.2,3.6)])
    assert p.recover_short_context(old,current,4.) is None
    p.move_window(0.)
    assert p.recover_short_context(old,current,5.) is None
    assert p.recover_short_context(old,current,5.6)==2
    assert ''.join(w.text for w in current[2:]).endswith(' no funciona')


def test_short_correction_accepts_a_tiny_resegmented_leading_word():
    p,old,current=short_correction()
    current[:1]=[AlignedWord(' Ha',0,.08),AlignedWord(' estat',.08,.32)]
    assert p.recover_short_context(old,current,3.) is None
    assert p.recover_short_context(old,current,3.6)==3
    assert p.confirmed==[' Ha',' estat',' fine']
    assert current[3].text==' tunejat'


def test_backend_short_prefix_revision_resumes_without_skipping_or_republishing():
    c=client();p,old,current=short_correction();c.stable_prefix=p
    c._fast_last_commit=(np.zeros(3*c.RATE,dtype=np.float32),0.,''.join(w.text for w in old))
    c._fast_samples=np.zeros(4*c.RATE,dtype=np.float32)
    c._fast_align=MagicMock(side_effect=[current,old,current,old])
    result=[NS(text=''.join(w.text for w in current),no_speech_prob=.1,avg_logprob=-.2)]
    c.handle_transcription_output(result,3.)
    assert not c.exit and p.confirmed==[w.text for w in old[:2]]
    c.handle_transcription_output(result,3.6)
    assert not c.exit and c.timestamp_offset==0.
    assert p.confirmed==[w.text for w in current[:2]]
    assert p.history==[{'sequence':1,'text':'estat fa'}]
    assert p.draft.startswith('tunejat')
    c.handle_transcription_output(result,4.2)
    assert p.history[-1]['text']=='tunejat a partir'
    c.websocket.close.assert_not_called()


def test_window_move_removes_only_acoustically_confirmed_context():
    p = FastTextPrefix()
    p.confirmed = [' una', ' prova']
    p.timed = [AlignedWord(' una', 0, .4), AlignedWord(' prova', .4, .8)]
    p.move_window(.4)
    assert p.confirmed == [' prova']
    assert p.prefix_length(words(' prova amb una cua')) == 1


def test_trim_keeps_agreement_for_retained_unconfirmed_audio():
    p = FastTextPrefix()
    text = words(' una prova nova que continua')
    p.observe(text, 9)
    p.observe(text, 10)
    p.timed = [AlignedWord(w, i, i + 1) for i, w in enumerate(p.confirmed)]
    p.move_window(3)
    assert p.confirmed == []
    # A cropped window may already exceed the soft limit because alignment
    # ran while audio kept arriving. Its existing tail is still valid evidence.
    assert ''.join(p.observe(words(' que continua amb més text'), 12)) == ' que continua'


def test_same_audio_after_trim_cannot_confirm_itself():
    p = FastTextPrefix()
    p.observe(words(' una prova que continua'), 10)
    p.move_window(2)
    assert p.observe(words(' una prova que continua'), 10) == []


def client():
    value = Backend.__new__(Backend)
    ServeClientBase.__init__(value, 'fast-unit', MagicMock(), translation_queue=None,
                             max_uncommitted_audio_s=10, context_overlap_s=5)
    value.stable_prefix = FastTextPrefix()
    value._fast_last_commit = None
    value._fast_samples = MagicMock()
    value.use_vad = False
    return value


def test_ordinary_decode_output_does_not_request_alignment():
    c = client()
    c._fast_align = MagicMock(side_effect=AssertionError('unexpected alignment'))
    segments = [NS(text=' Una prova general que continua', no_speech_prob=.1, avg_logprob=-.2)]
    c.handle_transcription_output(segments, 2)
    c.handle_transcription_output(segments, 3)
    c._fast_align.assert_not_called()
    assert c.stable_prefix.history[0]['text'] == 'Una prova general'
    assert c.timestamp_offset == 0


def test_window_with_few_closed_words_keeps_recovery_context():
    c = client()
    text = ' Una prova general que continua'
    aligned = [AlignedWord(w, i, i+1) for i, w in enumerate(words(text))]
    c._fast_align = MagicMock(return_value=aligned)
    segments = [NS(text=text, no_speech_prob=.1, avg_logprob=-.2)]
    c.handle_transcription_output(segments, 8)
    c.handle_transcription_output(segments, 10)
    c._fast_align.assert_called_once()
    assert c.timestamp_offset == 0
    assert c.stable_prefix.confirmed == words(' Una prova general')


def test_window_trim_retains_a_confirmed_phrase_as_well_as_time_overlap():
    c=client()
    text='una prova general que continua amb més context abans del final'
    aligned=[AlignedWord(w,i*.5,(i+1)*.5) for i,w in enumerate(words(text))]
    c._fast_align=MagicMock(return_value=aligned)
    result=[NS(text=text,no_speech_prob=.1,avg_logprob=-.2)]
    c.handle_transcription_output(result,8.)
    c.handle_transcription_output(result,10.)
    assert c.timestamp_offset==2.5
    assert c.stable_prefix.confirmed==words(' amb més context abans')
    assert c.stable_prefix.draft=='del final'


def test_unsafe_context_stops_research_instead_of_dropping_speech():
    c = client()
    c.stable_prefix.confirmed = [' una', ' prova']
    c._fast_align = MagicMock(return_value=[AlignedWord(' altre', 0, 1)])
    with pytest.raises(PrefixContextLost):
        c.handle_transcription_output([NS(text=' altre text', no_speech_prob=.1)], 20)
    assert c.exit
    c.websocket.close.assert_called_once()


def test_transient_context_mismatch_keeps_audio_and_waits_for_new_evidence():
    c = client()
    c.stable_prefix.confirmed = [' una', ' prova']
    c._fast_align = MagicMock(return_value=[AlignedWord(' altre', 0, 1)])
    c.handle_transcription_output([NS(text=' altre text', no_speech_prob=.1)], 10)
    assert not c.exit and c.timestamp_offset == 0
    assert c.stable_prefix.confirmed == [' una', ' prova']
    c.websocket.close.assert_not_called()


def test_fully_confirmed_short_utterance_retires_only_its_verified_silent_snapshot():
    c = client()
    c.use_vad = True
    c.vad_parameters = {'threshold': .5}
    c.timestamp_offset = 62.
    c._fast_samples = np.zeros(round(7.944*c.RATE), dtype=np.float32)
    # More audio arrived while the model decoded the snapshot.
    c.frames_offset = 60.
    c.frames_np = np.ones(12*c.RATE, dtype=np.float32)
    c.stable_prefix.confirmed = words('un llibre que continua fins')
    c.stable_prefix.previous = words('al final.')
    c.stable_prefix.previous_end = 69.
    c._fast_align = MagicMock(side_effect=AssertionError('silence boundary needs no alignment'))
    result = [NS(text='un llibre que continua fins al final.', no_speech_prob=.1, avg_logprob=-.2)]
    with patch('faster_whisper.vad.get_speech_timestamps', return_value=[{'start':0,'end':round(6.9*c.RATE)}]):
        c.handle_transcription_output(result,7.944)
    assert c.timestamp_offset == pytest.approx(69.944)
    assert not c.stable_prefix.confirmed and not c.stable_prefix.timed
    assert c.stable_prefix.history[-1]['text']=='al final.'
    assert c.frames_offset + len(c.frames_np)/c.RATE == 72.
    assert len(c.get_audio_chunk_for_processing()[0])/c.RATE == pytest.approx(72.-69.944, abs=1/c.RATE)
    c._fast_align.assert_not_called()


@pytest.mark.parametrize('voice_end,tail,segments', [
    (7.9, 'al final.', None),  # Speech continues at the snapshot end.
    (6.9, 'un final diferent.', None),  # New tail lacks agreement.
    (6.9, 'al final.', [NS(text='altres paraules', no_speech_prob=.99, avg_logprob=-2)]),
])
def test_short_snapshot_retirement_requires_pause_full_agreement_and_no_rejected_tail(voice_end,tail,segments):
    c = client(); c.use_vad=True; c.vad_parameters={'threshold':.5}
    c._fast_samples=np.zeros(round(7.944*c.RATE),dtype=np.float32)
    c.stable_prefix.confirmed=words('un llibre que continua fins')
    c.stable_prefix.previous=words('al final.');c.stable_prefix.previous_end=7.
    result=[NS(text='un llibre que continua fins '+tail,no_speech_prob=.1,avg_logprob=-.2)]+(segments or [])
    with patch('faster_whisper.vad.get_speech_timestamps',return_value=[{'start':0,'end':round(voice_end*c.RATE)}]):
        c.handle_transcription_output(result,7.944)
    assert c.timestamp_offset==0.


def test_short_pause_retirement_preserves_a_genuine_repeated_utterance():
    c=client();c.use_vad=True;c.vad_parameters={'threshold':.5}
    c._fast_align=MagicMock(side_effect=AssertionError('no alignment needed at a verified pause'))
    speech=[NS(text='una prova',no_speech_prob=.1,avg_logprob=-.2)]
    for _ in range(2):
        for duration in [2.,3.]:
            c._fast_samples=np.zeros(round(duration*c.RATE),dtype=np.float32)
            with patch('faster_whisper.vad.get_speech_timestamps',return_value=[{'start':0,'end':24000}]):
                c.handle_transcription_output(speech,duration)
    assert c.timestamp_offset==6.
    assert c.stable_prefix.history==[{'sequence':1,'text':'una prova'},{'sequence':2,'text':'una prova'}]


def test_first_long_window_defers_without_aligning_or_discarding_speech():
    c = client()
    c._fast_align = MagicMock(side_effect=AssertionError('nothing confirmed to trim'))
    c.handle_transcription_output([NS(text=' una prova nova que continua', no_speech_prob=.1)], 10.01)
    assert not c.exit and c.timestamp_offset == 0
    c._fast_align.assert_not_called()
    assert c.stable_prefix.draft == 'una prova nova que continua'


def test_unconfirmed_window_cannot_grow_indefinitely():
    c = client()
    with pytest.raises(PrefixContextLost, match='hard audio limit'):
        c.handle_transcription_output([NS(text=' una prova nova', no_speech_prob=.1)], 20)
    assert c.exit and c.timestamp_offset == 0


def test_incompatible_client_is_rejected_before_loading_model(monkeypatch):
    monkeypatch.setenv('WHISPER_BSC_STABLE_PREFIX', 'fast_text_prefix')
    with patch.object(Backend, 'create_model') as load:
        with pytest.raises(ModelSelectionError, match='compatible'):
            Backend(MagicMock(), model=BSC_MODEL, language='ca', max_uncommitted_audio_s=10)
        load.assert_not_called()


@pytest.mark.parametrize('model', ['small', 'Systran/faster-whisper-small',
                                  'large-v3', 'Systran/faster-whisper-large-v3', BSC_MODEL])
def test_release_profile_negotiates_prefix_for_every_supported_model(monkeypatch, model):
    monkeypatch.setenv('WHISPER_PREFIX_MODELS', 'small,systran-large,bsc-large')
    monkeypatch.delenv('WHISPER_BSC_STABLE_PREFIX', raising=False)
    monkeypatch.delenv('WHISPER_EXPERIMENTAL_PREFIX_MODELS', raising=False)
    ws = MagicMock()
    with patch.object(Backend, 'create_model'), patch('torch.cuda.is_available', return_value=False), \
            patch('whisper_live.backend.faster_whisper_backend.threading.Thread'):
        c = Backend(ws, model=model, language='ca', max_uncommitted_audio_s=10,
                    caption_protocols=['stable_text_prefix_v1'])
    ready = __import__('json').loads(ws.send.call_args.args[0])
    assert isinstance(c.stable_prefix, FastTextPrefix)
    assert ready['caption_protocol'] == 'stable_text_prefix_v1'
    assert ready['model'] == model


def test_release_policy_is_scoped_and_explicit_control_overrides_legacy(monkeypatch):
    monkeypatch.setenv('WHISPER_BSC_STABLE_PREFIX', 'fast_text_prefix')
    monkeypatch.setenv('WHISPER_EXPERIMENTAL_PREFIX_MODELS', 'small,systran-large')
    monkeypatch.setenv('WHISPER_PREFIX_MODELS', 'bsc-large')
    assert configured_fast_prefix(BSC_MODEL)
    assert not configured_fast_prefix('small')
    assert not configured_fast_prefix('tiny')
    monkeypatch.setenv('WHISPER_PREFIX_MODELS', 'none')
    assert not configured_fast_prefix(BSC_MODEL)
    assert not configured_fast_prefix('Systran/faster-whisper-large-v3')
    monkeypatch.setenv('WHISPER_PREFIX_MODELS', '')
    assert configured_fast_prefix(BSC_MODEL)  # Historical experiments remain reproducible.


@pytest.mark.parametrize('value', ['all', 'small,typo', 'none,small', ','])
def test_invalid_release_prefix_policy_fails_before_model_loading(monkeypatch, value):
    monkeypatch.setenv('WHISPER_PREFIX_MODELS', value)
    with patch.object(Backend, 'create_model') as load:
        with pytest.raises(ModelSelectionError, match='Prefix models'):
            Backend(MagicMock(), model='small', language='ca', max_uncommitted_audio_s=10)
        load.assert_not_called()


def test_release_policy_keeps_protocol_admission_guard(monkeypatch):
    monkeypatch.setenv('WHISPER_PREFIX_MODELS', 'systran-large')
    with patch.object(Backend, 'create_model') as load:
        with pytest.raises(ModelSelectionError, match='compatible'):
            Backend(MagicMock(), model='large-v3', language='ca', max_uncommitted_audio_s=10)
        load.assert_not_called()


@pytest.mark.parametrize('selected,model,enabled', [
    ('', 'small', False), ('', 'Systran/faster-whisper-large-v3', False),
    ('small', 'small', True), ('small', 'Systran/faster-whisper-small', True),
    ('small', 'Systran/faster-whisper-large-v3', False),
    ('systran-large', 'small', False),
    ('systran-large', 'Systran/faster-whisper-large-v3', True),
    (' small, systran-large ', 'large-v3', True),
    ('small,systran-large', BSC_MODEL, False),
])
def test_experimental_prefix_selection_is_explicit_and_model_scoped(monkeypatch, selected, model, enabled):
    monkeypatch.setenv('WHISPER_EXPERIMENTAL_PREFIX_MODELS', selected)
    assert experimental_fast_prefix(model) is enabled


@pytest.mark.parametrize('selected', ['all', 'medium', 'bsc-large', 'small,typo'])
def test_experimental_prefix_typo_is_not_silently_ignored(monkeypatch, selected):
    monkeypatch.setenv('WHISPER_EXPERIMENTAL_PREFIX_MODELS', selected)
    with patch.object(Backend, 'create_model') as load:
        with pytest.raises(ModelSelectionError, match='small or systran-large'):
            Backend(MagicMock(), model='small', language='ca', max_uncommitted_audio_s=10)
        load.assert_not_called()


@pytest.mark.parametrize('model,selected', [
    ('small', 'small'), ('Systran/faster-whisper-large-v3', 'systran-large')])
@pytest.mark.parametrize('incompatible', [
    {'caption_protocols': []}, {'clip_audio': True}, {'max_uncommitted_audio_s': 0},
    {'translation_queue': MagicMock()}, {'task': 'translate'}, {'language': None},
])
def test_experimental_models_keep_prefix_admission_guards(monkeypatch, model, selected, incompatible):
    monkeypatch.setenv('WHISPER_EXPERIMENTAL_PREFIX_MODELS', selected)
    options = dict(model=model, language='ca', max_uncommitted_audio_s=10,
                   caption_protocols=['stable_text_prefix_v1'])
    options.update(incompatible)
    with patch.object(Backend, 'create_model') as load:
        with pytest.raises(ModelSelectionError, match='compatible'):
            Backend(MagicMock(), **options)
        load.assert_not_called()


@pytest.mark.parametrize('model,selected,expected', [
    ('small', '', False), ('Systran/faster-whisper-large-v3', '', False),
    ('small', 'small', True), ('Systran/faster-whisper-large-v3', 'systran-large', True),
    ('small', 'systran-large', False), ('Systran/faster-whisper-large-v3', 'small', False),
    (BSC_MODEL, '', True),
])
def test_prefix_negotiation_matches_selected_model(monkeypatch, model, selected, expected):
    # Existing BSC opt-in must not implicitly enable Systran or be shadowed.
    monkeypatch.setenv('WHISPER_BSC_STABLE_PREFIX', 'fast_text_prefix')
    monkeypatch.setenv('WHISPER_EXPERIMENTAL_PREFIX_MODELS', selected)
    ws = MagicMock()
    with patch.object(Backend, 'create_model'), patch('torch.cuda.is_available', return_value=False), \
            patch('whisper_live.backend.faster_whisper_backend.threading.Thread'):
        c = Backend(ws, model=model, language='ca', max_uncommitted_audio_s=10,
                    caption_protocols=['stable_text_prefix_v1'])
    ready = __import__('json').loads(ws.send.call_args.args[0])
    assert isinstance(c.stable_prefix, FastTextPrefix) is expected
    assert (ready.get('caption_protocol') == 'stable_text_prefix_v1') is expected
    assert ready['model'] == model


def test_attached_batch_worker_cannot_silently_change_prefix_protocol():
    c = client()
    with patch.object(Backend, 'BATCH_WORKER', MagicMock()):
        with pytest.raises(PrefixContextLost, match='batch worker'):
            c.transcribe_audio(MagicMock())
    assert c.exit
    c.websocket.close.assert_called_once()


def test_forced_alignment_never_decodes_text_again_and_keeps_raw_audio_clock():
    model = MagicMock()
    model.feature_extractor.sampling_rate = 16000
    model.feature_extractor.nb_max_frames = 3000
    model.feature_extractor.return_value = np.zeros((80, 301), dtype=np.float32)
    model.max_length = 448
    model.find_alignment.return_value = [[{'word': ' una', 'start': .2, 'end': .6},
                                          {'word': ' prova', 'start': .6, 'end': 1.1}]]
    with patch('faster_whisper.tokenizer.Tokenizer') as tokenizer:
        tokenizer.return_value.encode.return_value = [1, 2, 3]
        tokenizer.return_value.sot_sequence = [4, 5]
        aligned = align_text(model, np.zeros(48000, dtype=np.float32), 'una prova', 'ca', 10)
    model.transcribe.assert_not_called()
    assert ''.join(w.text for w in aligned) == ' una prova'
    assert (aligned[0].start, aligned[-1].end) == (10.2, 11.1)
    assert model.find_alignment.call_args.args[-1] == 300


def test_forced_alignment_cannot_silently_drop_a_decoded_word():
    model = MagicMock()
    model.feature_extractor.sampling_rate = 16000
    model.feature_extractor.nb_max_frames = 3000
    model.feature_extractor.return_value = np.zeros((80, 301), dtype=np.float32)
    model.max_length = 448
    model.find_alignment.return_value = [[{'word': ' una', 'start': .2, 'end': .6}]]
    with patch('faster_whisper.tokenizer.Tokenizer') as tokenizer:
        tokenizer.return_value.encode.return_value = [1, 2, 3]
        tokenizer.return_value.sot_sequence = [4, 5]
        with pytest.raises(PrefixContextLost, match='inconsistent'):
            align_text(model, np.zeros(48000, dtype=np.float32), 'una prova', 'ca')


def test_zero_duration_context_word_uses_unique_overlapping_neighbours():
    p = FastTextPrefix()
    p.confirmed = [' un', ' context', ' de', ' prova']
    p.timed = [AlignedWord(' un', 0, .3), AlignedWord(' context', .3, .8),
               AlignedWord(' de', .8, 1), AlignedWord(' prova', 1, 1.2)]
    aligned = [AlignedWord(' fragment', 0, .1), AlignedWord(' un', .1, .3),
               AlignedWord(' context', .3, .8), AlignedWord(' de', .8, 1.18),
               AlignedWord(' prova', 1.18, 1.18), AlignedWord(' nova', 1.18, 1.6)]
    assert p.align_context(aligned) == 5
    assert p.confirmed[-1] == ' prova'


def test_zero_duration_words_without_overlapping_neighbours_cannot_drop_new_speech():
    p = FastTextPrefix()
    p.confirmed = [' un', ' context', ' de', ' prova']
    p.timed = [AlignedWord(w, 1, 1) for w in p.confirmed]
    aligned = [AlignedWord(' fragment', 0, 1)] + p.timed + [AlignedWord(' nova', 1, 2)]
    assert p.align_context(aligned) is None


def test_short_rewritten_context_is_located_by_shared_audio_and_unique_phrase():
    p = FastTextPrefix()
    old = words(' un breu resum de la bibliografia de la')
    p.confirmed = old
    p.timed = [AlignedWord(w, i*.2, (i+1)*.2) for i, w in enumerate(old[:5])]
    p.timed += [AlignedWord(' bibliografia', 1., 1.8), AlignedWord(' de', 1.8, 2.),
                AlignedWord(' la', 2., 2.1)]
    current = [AlignedWord(w, i*.2, (i+1)*.2) for i, w in enumerate(words(' un breu resum de la'))]
    current += [AlignedWord(' biografia', 1., 2.54), AlignedWord(' de', 2.54, 2.74),
                AlignedWord(' la', 2.74, 2.9), AlignedWord(' persona', 2.9, 3.2)]
    assert p.align_context(current) == 6
    # Words spoken after the old closed audio endpoint remain draft/new text.
    assert p.confirmed[-1] == ' biografia'


def test_same_rewritten_phrase_in_later_audio_is_not_suppressed():
    p = FastTextPrefix()
    old = words(' un breu resum de la bibliografia de la')
    p.confirmed = old
    p.timed = [AlignedWord(w, i*.2, (i+1)*.2) for i, w in enumerate(old)]
    current = [AlignedWord(w, 2+i*.2, 2+(i+1)*.2)
               for i, w in enumerate(words(' un breu resum de la biografia nova'))]
    assert p.align_context(current) is None


def test_distinctive_pair_with_majority_overlap_survives_corrected_article():
    p = FastTextPrefix()
    p.confirmed = [' al', ' cognom', ' compost']
    p.timed = [AlignedWord(' al', 0, .2), AlignedWord(' cognom', .2, .54),
               AlignedWord(' compost', .54, 1.4)]
    current = [AlignedWord(' el', 0, .2), AlignedWord(' cognom', .2, .56),
               AlignedWord(' compost', .56, 1.08), AlignedWord(' continua', 1.08, 1.6)]
    assert p.align_context(current) == 3


def test_later_distinctive_pair_with_only_small_overlap_is_not_old_context():
    p = FastTextPrefix()
    p.confirmed = [' al', ' cognom', ' compost']
    p.timed = [AlignedWord(' al', 0, .2), AlignedWord(' cognom', .2, .54),
               AlignedWord(' compost', .54, 1.4)]
    current = [AlignedWord(' el', .3, .5), AlignedWord(' cognom', .5, .84),
               AlignedWord(' compost', .84, 1.7), AlignedWord(' continua', 1.7, 2.)]
    assert p.align_context(current) is None


@pytest.mark.parametrize('phrase', ['aquest llibre', 'projecte general', 'resultat negatiu'])
def test_corrected_tail_recovers_from_unique_phrase_with_redistributed_word_times(phrase):
    p = FastTextPrefix()
    first, second = words(phrase)
    p.confirmed = [first, second, ' etiqueta', ' antiga']
    p.timed = [AlignedWord(first, 55.66, 56.44), AlignedWord(second, 56.44, 57.54),
               AlignedWord(' etiqueta', 57.54, 58.62), AlignedWord(' antiga', 58.62, 59.26)]
    p.history = [{'sequence': 1, 'text': ''.join(p.confirmed).strip()}]
    history = list(p.history)
    current = [AlignedWord(first, 55.66, 57.16), AlignedWord(second, 57.16, 57.54),
               AlignedWord(' altra', 57.54, 58.4), AlignedWord(' etiqueta', 58.4, 59.22),
               AlignedWord(' continua', 59.22, 59.6)]
    assert p.align_context(current) == 4
    assert p.confirmed == [w.text for w in current[:4]]
    assert current[4].text == ' continua'
    assert p.history == history  # Reconcile retained context, never revise published text.


@pytest.mark.parametrize('repeated_side', ['old', 'new'])
def test_phrase_span_recovery_rejects_repeated_anchor(repeated_side):
    p = FastTextPrefix()
    old = words('projecte general etiqueta antiga')
    current = words('projecte general altra etiqueta continua')
    if repeated_side == 'old':
        old = words('projecte general projecte general etiqueta antiga')
    else:
        current = words('projecte general altra etiqueta projecte general continua')
    p.confirmed = old
    p.timed = [AlignedWord(w, i*.4, (i+1)*.4) for i, w in enumerate(old)]
    aligned = [AlignedWord(w, i*.4, (i+1)*.4) for i, w in enumerate(current)]
    assert p.align_context(aligned) is None


def test_phrase_span_recovery_preserves_new_negation_after_closed_tail():
    p = FastTextPrefix()
    p.confirmed = words('projecte general etiqueta antiga')
    p.timed = [AlignedWord(w, start, end) for w, start, end in zip(
        p.confirmed, [0, .78, 1.88, 2.96], [.78, 1.88, 2.96, 3.6])]
    current = [AlignedWord(w, start, end) for w, start, end in zip(
        words('projecte general altra etiqueta no funciona'),
        [0, 1.5, 1.88, 2.74, 3.6, 3.9], [1.5, 1.88, 2.74, 3.56, 3.9, 4.4])]
    assert p.align_context(current) == 4
    assert ''.join(w.text for w in current[4:]).strip() == 'no funciona'


def test_phrase_span_recovery_cannot_bridge_an_inserted_negation():
    p = FastTextPrefix()
    p.confirmed = words('projecte general etiqueta antiga')
    p.timed = [AlignedWord(w, start, end) for w, start, end in zip(
        p.confirmed, [0, .78, 1.88, 2.96], [.78, 1.88, 2.96, 3.6])]
    current = [AlignedWord(w, start, end) for w, start, end in zip(
        words('projecte no general altra etiqueta continua'),
        [0, 1.5, 1.55, 1.88, 2.74, 3.56], [1.5, 1.55, 1.88, 2.74, 3.56, 4.0])]
    assert p.align_context(current) is None


@pytest.mark.parametrize('shift', [.3, 1.88])
def test_redistributed_phrase_at_a_different_audio_span_is_not_closed_context(shift):
    p = FastTextPrefix()
    p.confirmed = words('projecte general etiqueta antiga')
    p.timed = [AlignedWord(w, start, end) for w, start, end in zip(
        p.confirmed, [0, .78, 1.88, 2.96], [.78, 1.88, 2.96, 3.6])]
    current = [AlignedWord(w, start+shift, end+shift) for w, start, end in zip(
        words('projecte general altra etiqueta continua'),
        [0, 1.5, 1.88, 2.74, 3.56], [1.5, 1.88, 2.74, 3.56, 4.0])]
    assert p.align_context(current) is None


@pytest.mark.parametrize('retired_s', [0., 25.66, 40.])
def test_fast_audio_buffer_discards_only_retired_samples(retired_s):
    c = client()
    c.frames_offset = 30.
    c.timestamp_offset = 30. + retired_s
    original = np.arange(45*c.RATE+1, dtype=np.float32)
    c.frames_np = original.copy()
    extra = np.array([-1.], dtype=np.float32)
    c.add_frames(extra)
    count = min(30*c.RATE, int(retired_s*c.RATE))
    # Float rounding may conservatively retain one extra sample.
    actual = round((c.frames_offset-30.)*c.RATE)
    assert abs(actual-count) <= 1
    assert c.frames_offset <= c.timestamp_offset
    assert c.timestamp_offset == 30. + retired_s
    np.testing.assert_array_equal(c.frames_np, np.concatenate((original[actual:], extra)))


@pytest.mark.parametrize('policy', [None, RetainedWordPrefix()])
def test_other_policies_keep_legacy_audio_buffer_retirement(policy):
    c = client()
    c.stable_prefix = policy
    c.frames_offset = 30.
    c.timestamp_offset = 55.66
    c.frames_np = np.zeros(45*c.RATE+1, dtype=np.float32)
    c.add_frames(np.zeros(1, dtype=np.float32))
    assert c.frames_offset == 60.
    assert c.timestamp_offset == 60.


@pytest.mark.parametrize('model', [BSC_MODEL, 'small', 'Systran/faster-whisper-large-v3'])
@pytest.mark.parametrize('policy', ['fast', 'original', 'aligned'])
def test_only_bsc_fast_prefix_disables_native_timestamp_prediction(policy, model):
    c = client()
    c.model_size_or_path = model
    c.stable_prefix = {'fast': FastTextPrefix(), 'original': None,
                       'aligned': RetainedWordPrefix()}[policy]
    c.model_selection = None
    c.transcriber = MagicMock()
    c.transcriber.transcribe.return_value = ([], None)
    c.initial_prompt = None
    c.language = 'ca'
    c.task = 'transcribe'
    c.vad_parameters = {'threshold': .5}
    c.transcribe_audio(np.zeros(16000, dtype=np.float32))
    options = c.transcriber.transcribe.call_args.kwargs
    assert options.get('without_timestamps', False) == (policy == 'fast' and model == BSC_MODEL)
    assert options.get('word_timestamps', False) == (policy == 'aligned')


def test_rewritten_multilingual_length_tail_uses_audio_span_not_word_count():
    p = FastTextPrefix()
    old = words(' un discurs molt important que és el mot canviat en quatre parts')
    p.confirmed = old
    p.timed = [AlignedWord(w, i*.2, (i+1)*.2) for i, w in enumerate(old)]
    new = words(' un discurs molt important que és el word changed into several pieces continua')
    current = [AlignedWord(w, i*.2, (i+1)*.2) for i, w in enumerate(new)]
    assert p.align_context(current) == 12
    assert current[12].text == ' continua'  # Genuinely new speech stays unconfirmed.


def test_recovery_cannot_bridge_beyond_the_retained_audio_span():
    p = FastTextPrefix(max_recovery_span_s=.5)
    old = words(' un discurs molt important que és el mot canviat en quatre parts')
    p.confirmed = old
    p.timed = [AlignedWord(w, i*.2, (i+1)*.2) for i, w in enumerate(old)]
    new = words(' un discurs molt important que és el word changed into several pieces continua')
    current = [AlignedWord(w, i*.2, (i+1)*.2) for i, w in enumerate(new)]
    assert p.align_context(current) is None


def silence_client():
    c = client()
    c.use_vad = True
    c.vad_parameters = {'threshold': .5}
    c._fast_samples = np.zeros(round(10.2*c.RATE), dtype=np.float32)
    c.frames_np = np.zeros(11*c.RATE, dtype=np.float32)  # New audio arrived during inference.
    c.stable_prefix.confirmed = words(' una prova acabada')
    c.stable_prefix.sequence = 1
    c.stable_prefix.history = [{'sequence': 1, 'text': 'una prova acabada'}]
    aligned = [AlignedWord(w, i*.4, (i+1)*.4) for i, w in enumerate(c.stable_prefix.confirmed)]
    c._fast_align = MagicMock(return_value=aligned)
    return c


def test_fully_closed_speech_retires_verified_silence_without_leaving_a_phoneme():
    c = silence_client()
    with patch('faster_whisper.vad.get_speech_timestamps', return_value=[{'start': 0, 'end': 20800}]):
        c.handle_transcription_output([NS(text=' una prova acabada', no_speech_prob=.1)], 10.2)
    assert c.timestamp_offset == 10.2  # Not 1.2 (estimated word end), nor 11 (unprocessed new audio).
    assert c.stable_prefix.confirmed == []
    assert c.stable_prefix.history == [{'sequence': 1, 'text': 'una prova acabada'}]
    assert c.stable_prefix.sequence == 1


def test_unconfirmed_speech_is_never_discarded_as_silence():
    c = silence_client()
    with patch('faster_whisper.vad.get_speech_timestamps', return_value=[{'start': 0, 'end': 20800}]):
        c.handle_transcription_output([NS(text=' una prova acabada nova veu', no_speech_prob=.1)], 10.2)
    assert c.timestamp_offset == 0.  # Keep the short closed phrase as recovery context.
    assert c.stable_prefix.draft == 'nova veu'


def test_voice_at_snapshot_end_prevents_silence_retirement():
    c = silence_client()
    with patch('faster_whisper.vad.get_speech_timestamps', return_value=[{'start': 0, 'end': 161600}]):
        c.handle_transcription_output([NS(text=' una prova acabada', no_speech_prob=.1)], 10.2)
    assert c.timestamp_offset == pytest.approx(1.2)
