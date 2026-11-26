import pytest

from asr.caption_revisions import SourceCaptionRevisions
from asr.whisper_client import TranscriptionChunk


def chunk(text, start, end, reason="timeout", partial=False):
    return TranscriptionChunk(text=text, start_time=start, end_time=end,
                              finalization_reason=reason, is_partial=partial)


def test_tail_correction_preserves_unique_prefix_of_bounded_window():
    revisions = SourceCaptionRevisions()
    text = "I són els coneixements en aquestes dues branques el que fan tan particular la seva recerca, com veurem a continuació."
    revisions.receive(chunk(text, 57.261, 66.841, "bounded_window"))
    corrected = revisions.receive(chunk("en particular, a la seva recerca, com veurem a continuació.",
                                        62.497, 66.858, "stable"))
    assert corrected.text == "I són els coneixements en aquestes dues branques el que fan en particular, a la seva recerca, com veurem a continuació."
    assert corrected.start_time == 57.261
    assert revisions.draft is None


def test_long_exact_tail_at_low_audio_overlap_is_kept_in_the_same_draft():
    revisions = SourceCaptionRevisions()
    first = "i, o dit d'altra manera, per posar el gènere i les dones com a objecte de la recerca."
    revisions.receive(chunk(first, 148.841, 154.581))
    tail = 'com a objecte de la recerca en economia.'
    partial = revisions.receive(chunk(tail, 152.461, 155.881, partial=True))
    assert partial.text == first[:-1] + ' en economia.'
    extended = revisions.receive(chunk(tail, 152.461, 155.881))
    assert extended.text == partial.text
    assert extended.start_time == 148.841
    assert not revisions.closed
    final = revisions.receive(chunk(tail, 152.461, 155.881, 'stable'))
    assert final.text == extended.text
    assert final.text.count('com a objecte de la recerca') == 1
    assert [c.text for c in revisions.closed] == [final.text]


def test_long_tail_spoken_in_later_audio_remains_a_separate_utterance():
    revisions = SourceCaptionRevisions()
    first = chunk("Posem les dones com a objecte de la recerca.", 148, 152)
    revisions.receive(first)
    later = chunk('com a objecte de la recerca en economia.', 152, 156, 'stable')
    assert revisions.receive(later) is later
    assert list(revisions.closed) == [first, later]


def test_tail_partial_updates_draft_without_removing_its_prefix():
    revisions = SourceCaptionRevisions()
    text = "I són els coneixements en aquestes dues branques el que fan tan particular la seva recerca, com veurem a continuació."
    revisions.receive(chunk(text, 57.261, 66.841, "bounded_window"))
    partial = revisions.receive(chunk("en particular la seva recerca, com veurem a continuació.",
                                     62.497, 66.858, partial=True))
    assert partial.text.startswith("I són els coneixements")
    assert "fan en particular la seva recerca" in partial.text
    assert revisions.draft.text == text


def test_overlap_across_two_chunks_does_not_repeat_closed_context():
    revisions = SourceCaptionRevisions()
    first = "La Clàudia Golding guanya el Premi Nobel per avançar el nostre coneixement."
    second = "en els resultats de les dones al mercat de treball."
    revisions.receive(chunk(first, 138.751, 145.251))
    revisions.receive(chunk(second, 145.291, 148.391, "bounded_window"))
    repeated = revisions.receive(chunk("el nostre coneixement " + second, 143.482, 148.502))
    assert repeated is None
    assert revisions.draft.text == second
    assert [c.text for c in revisions.closed] == [first]


def test_new_words_after_a_cross_boundary_overlap_are_preserved():
    revisions = SourceCaptionRevisions()
    revisions.receive(chunk("Avancem el nostre coneixement.", 10, 13))
    revisions.receive(chunk("en els resultats de les dones.", 13, 16, "bounded_window"))
    updated = revisions.receive(chunk("el nostre coneixement en els resultats de les dones al mercat de treball.",
                                     12, 18, "stable"))
    assert updated.text == "en els resultats de les dones al mercat de treball."
    assert updated.start_time == 13


def test_actual_repetition_in_later_audio_is_preserved():
    revisions = SourceCaptionRevisions()
    first = chunk("El nostre coneixement.", 1, 3, "stable")
    second = chunk("El nostre coneixement.", 6, 8, "stable")
    assert revisions.receive(first) is first
    assert revisions.receive(second) is second


def test_ambiguous_overlap_preserves_both_texts():
    revisions = SourceCaptionRevisions()
    first = "Aquest és un context que no podem perdre."
    second = "Una alternativa completament diferent."
    revisions.receive(chunk(first, 0, 10, "bounded_window"))
    updated = revisions.receive(chunk(second, 5, 10, "stable"))
    assert first in updated.text and second in updated.text


def test_unrelated_new_audio_starts_its_own_draft():
    revisions = SourceCaptionRevisions()
    first = chunk("La primera idea.", 0, 3)
    second = chunk("La segona idea.", 4, 6)
    revisions.receive(first)
    assert revisions.receive(second) is second
    assert list(revisions.closed) == [first]


def test_short_hypothesis_with_shifted_start_is_replaced_without_repeating_name():
    revisions = SourceCaptionRevisions()
    revisions.receive(chunk("la Clàudia Golding...", 138.451, 140.286))
    full = "La Claudia Golding guanya el Premi Nobel."
    assert revisions.receive(chunk(full, 138.751, 141.871)).text == full


def test_interior_fragments_of_retained_window_are_not_appended_again():
    revisions = SourceCaptionRevisions()
    first = "Al final, i això també ho hem pactat amb el Jordi, parlaré una mica de l'estat de la qüestió en la professió, sobre quin és l'estat de la qüestió matèria."
    revisions.receive(chunk(first, 4.780, 14.221, "bounded_window"))
    assert revisions.receive(chunk("la qüestió en la", 9.8, 11.201, "stable")) is None
    assert revisions.receive(chunk("professió, sobre quin és", 11.201, 13.001, "stable")) is None
    tail = revisions.receive(chunk("la qüestió matèria de gènere.", 13.001, 14.921))
    assert tail.text == first[:-1] + " de gènere."
    final = revisions.receive(chunk("de la qüestió matèria de gènere.", 13.001, 14.941, "stable"))
    assert final.text == first[:-1] + " de gènere."
    assert final.text.count("en la professió") == 1


def test_changed_word_inside_retained_window_is_not_discarded():
    revisions = SourceCaptionRevisions()
    revisions.receive(chunk("La investigadora pot publicar aquest treball avui.", 0, 10, "bounded_window"))
    corrected = revisions.receive(chunk("la investigadora no pot publicar", 1, 5, "stable"))
    assert corrected is not None
    assert "no pot publicar" in corrected.text


def test_contained_words_spoken_after_retained_window_are_not_discarded():
    revisions = SourceCaptionRevisions()
    revisions.receive(chunk("Parlem de la qüestió en la professió.", 0, 10, "bounded_window"))
    repeated = chunk("la qüestió en la professió", 12, 15, "stable")
    assert revisions.receive(repeated) is repeated


def test_plain_timeout_chunks_keep_their_existing_stable_segmentation():
    revisions = SourceCaptionRevisions()
    revisions.receive(chunk("i l'any 2023 és el torn de la Clàudia Goldin.", 108.528, 113.708))
    first = chunk("i l'any 2023", 108.528, 110.708, "stable")
    second = chunk("és el torn", 110.708, 111.908, "stable")
    third = chunk("de la Clàudia Goldin", 111.908, 113.708, "stable")
    assert [revisions.receive(c).text for c in (first, second, third)] == [first.text, second.text, third.text]


def test_truncated_name_at_short_overlap_keeps_year_and_only_latest_name():
    revisions = SourceCaptionRevisions()
    revisions.receive(chunk("La primera va ser l'any 2009.", 72.876, 75.156))
    revisions.receive(chunk("La primera va ser l'any 2009, la Elinor Om.", 72.876, 76.596))
    expanded = revisions.receive(chunk("l'Elinor Armstrong, que juntament amb l'Oliver Witt",
                                       75.716, 78.783))
    assert expanded.text == "La primera va ser l'any 2009, l'Elinor Armstrong, que juntament amb l'Oliver Witt"
    final = revisions.receive(chunk("la Elinor Armstrong, que juntament amb l'Oliver Williamson",
                                    75.316, 79.476, "stable"))
    assert final.text == "La primera va ser l'any 2009, la Elinor Armstrong, que juntament amb l'Oliver Williamson"
    assert "Elinor Om" not in final.text
    assert final.start_time == 72.876


def test_shared_name_in_later_audio_is_not_folded_into_earlier_draft():
    revisions = SourceCaptionRevisions()
    revisions.receive(chunk("La primera va ser la Elinor Om.", 70, 75))
    later = chunk("la Elinor Armstrong va parlar després.", 76, 79)
    assert revisions.receive(later) is later


def test_common_lowercase_word_does_not_authorize_a_short_overlap_splice():
    revisions = SourceCaptionRevisions()
    first = chunk("Parlem sobre la recerca avui.", 0, 4)
    revisions.receive(first)
    later = chunk("la recerca necessita més recursos.", 3, 8)
    assert revisions.receive(later) is later


def test_already_complete_name_in_short_overlap_is_not_repeated():
    revisions = SourceCaptionRevisions()
    revisions.receive(chunk("La primera va ser l'any 2009, la Elinor Armstrong.", 72.876, 76.596))
    result = revisions.receive(chunk("l'Elinor Armstrong, que juntament amb l'Oliver Williamson",
                                     75.716, 79.476, "stable"))
    assert result.text == "La primera va ser l'any 2009, l'Elinor Armstrong, que juntament amb l'Oliver Williamson"


@pytest.mark.parametrize('first,continuation,expected', [
    ('Aquest estudi analitza el mercat laboral.', 'mercat laboral de les dones.',
     'Aquest estudi analitza el mercat laboral de les dones.'),
    ('Parlem de la governan.', 'la governança econòmica.',
     'Parlem de la governança econòmica.'),
    ('La proposta incorpora tecnolo.', 'tecnologies aplicades a la recerca.',
     'La proposta incorpora tecnologies aplicades a la recerca.'),
    ('Parlem del mercat labural.', 'mercat laboral i dels salaris.',
     'Parlem del mercat laboral i dels salaris.'),
    ('The report describes economic gover.', 'economic governance in Europe.',
     'The report describes economic governance in Europe.'),
    ('En aquest cas la clàudia goldin.', 'clàudia goldin presenta la recerca.',
     'En aquest cas la clàudia goldin presenta la recerca.'),
    ('Aquest estudi tracta el mercat laboral europe.', 'el mercat laboral europeu.',
     'Aquest estudi tracta el mercat laboral europeu.'),
    ('El taller explica la configura.', 'la configuración de la red.',
     'El taller explica la configuración de la red.'),
    ('We are checking packet reassem.', 'packet reassembly under load.',
     'We are checking packet reassembly under load.'),
    ('La ruta segueix el camí forest.', 'el camí forestal fins al refugi.',
     'La ruta segueix el camí forestal fins al refugi.'),
    ('A la cuina fem una crema de carab.', 'de carabassa amb espècies.',
     'A la cuina fem una crema de carabassa amb espècies.'),
    ("Parlem de l'administra.", 'administració pública.',
     "Parlem de l'administració pública."),
    ('Necessitem d’informa.', 'informatització dels processos.',
     'Necessitem d’informatització dels processos.'),
    ('El mecanisme és anti-infla.', 'inflacionari.',
     'El mecanisme és anti-inflacionari.'),
])
def test_short_tail_correction_is_not_limited_to_capitalized_names(first, continuation, expected):
    revisions = SourceCaptionRevisions()
    revisions.receive(chunk(first, 0, 5))
    partial = revisions.receive(chunk(continuation, 4, 8, partial=True))
    assert partial.text == expected
    assert revisions.draft.text == first
    final = revisions.receive(chunk(continuation, 4, 8, 'stable'))
    assert final.text == expected
    assert final.start_time == 0
    assert final.end_time == 8
    assert revisions.draft is None


@pytest.mark.parametrize('first,continuation', [
    ('Ens interessa el mercat laboral.', 'mercat laboral de les dones.'),
    ('La proposta incorpora tecnolo.', 'tecnologies aplicades a la recerca.'),
])
def test_generalized_short_tail_preserves_repetitions_in_later_audio(first, continuation):
    revisions = SourceCaptionRevisions()
    previous = chunk(first, 0, 5)
    revisions.receive(previous)
    later = chunk(continuation, 5, 9, 'stable')
    assert revisions.receive(later) is later
    assert list(revisions.closed) == [previous, later]


@pytest.mark.parametrize('first,continuation', [
    ('Parlem de la recerca avui.', 'la recerca necessita recursos.'),
    ('La proposta és una cosa.', 'una cosa diferent.'),
    ('Ara fem això en la.', 'en la següent sessió.'),
    ('La proposta inclou cat.', 'català i anglès.'),
    ('La governança és important, la governan.', 'la governança econòmica.'),
    ('Hi ha mercat laboral, i un altre mercat laboral.', 'mercat laboral de les dones.'),
    ('El debat és públic.', 'és privat i confidencial.'),
    ('La recerca no.', 'recerca nova sobre salaris.'),
    ('Aquest estudi analitza el mercat laboral.', 'no mercat laboral sinó salaris.'),
    ('La proposta inclou investigació.', 'investigació sobre salaris.'),
])
def test_weak_or_ambiguous_short_tail_does_not_authorize_deletion(first, continuation):
    revisions = SourceCaptionRevisions()
    revisions.receive(chunk(first, 0, 5))
    later = chunk(continuation, 4, 8, 'stable')
    assert revisions.receive(later) is later


@pytest.mark.parametrize('start,end,next_start,next_end', [
    (0, 2, 1.4, 4.5),
    (50, 60, 58.4, 65),
    (3600, 3604, 3603.7, 3609),
    (0, 1.2, 0.9, 3.5),
])
def test_short_tail_uses_relative_audio_spans_with_variable_chunk_lengths(start, end, next_start, next_end):
    revisions = SourceCaptionRevisions()
    revisions.receive(chunk('A la cuina fem una crema de carab.', start, end))
    corrected = revisions.receive(chunk('de carabassa amb espècies.', next_start, next_end, 'stable'))
    assert corrected.text == 'A la cuina fem una crema de carabassa amb espècies.'
    assert corrected.start_time == start
    assert corrected.end_time == next_end


def test_missing_audio_timing_does_not_authorize_a_text_only_splice():
    revisions = SourceCaptionRevisions()
    first = chunk('La proposta incorpora tecnolo.', 0, 0)
    revisions.receive(first)
    later = chunk('tecnologies aplicades a la recerca.', 0, 0, 'stable')
    assert revisions.receive(later) is later
    assert list(revisions.closed) == [first, later]
