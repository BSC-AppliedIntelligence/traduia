"""Browser checks with synthetic messages only; never connect to live services."""
import importlib.util
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def page():
    spec = importlib.util.spec_from_file_location("caption_preview", ROOT / "scripts/preview_stage_captions.py")
    preview = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preview)
    with playwright.sync_playwright() as browser_tools:
        browser = browser_tools.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1000, "height": 850})
        page.route("**/*", lambda route: route.abort())
        page.set_content(preview.preview_html())
        # Measure the settled responsive layout, not channel-button padding
        # halfway through its existing hover/resize transition.
        page.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; }")
        page.evaluate("clearInterval(timer); original.reset(); translation.reset()")
        yield page
        browser.close()


def send(page, text, sequence, *, partial=False, key="", closed=True, chunk_id=None, **extra):
    page.evaluate("data => original.receive(data)", {
        "text": text, "caption_sequence": sequence, "stream_id": "test-stream",
        "is_partial": partial, "revision_key": key, "revision_closed": closed,
        "chunk_id": chunk_id or str(sequence), **extra,
    })


def test_partial_and_timeout_revisions_only_change_draft(page):
    send(page, "Primer fragment.", 1)
    page.evaluate("window.firstParagraph = original.history.firstElementChild")
    send(page, "Una hipòtesi", 2, partial=True, closed=False)
    send(page, "Una hipòtesi provisional", 3, key="draft-1", closed=False)
    send(page, "Una hipòtesi corregida", 4, partial=True, key="draft-1", closed=False)
    assert page.locator("#originalSubtitle .caption-history p").all_text_contents() == ["Primer fragment."]
    assert page.locator("#originalSubtitle .caption-draft p").all_text_contents() == ["Una hipòtesi corregida"]
    assert page.evaluate("original.history.firstElementChild === firstParagraph")


def test_asr_failure_notice_remains_for_failed_stream_and_clears_for_new_source(page):
    template=(ROOT/'templates/stage.html').read_text()
    notice=template[template.index("let failedAsrStream =") : template.index('async function init()')]
    handler=template[template.index('function handleSubtitle(data)') : template.index('function clearSubtitles()')]
    page.add_script_tag(content='const originalCaptions=original;const translationCaptions=translation;'
        +"function setPlayerStatus(text){document.getElementById('statusText').textContent=text;}"+notice+handler)
    page.evaluate("showAsrFailure({message:'Transcription stopped. Please restart.',stream_id:'failed'})")
    assert page.locator('#asrError').is_visible()
    assert 'Transcription stopped' in page.locator('#asrError').inner_text()
    page.evaluate("handleSubtitle({channel:'original',text:'old tail',stream_id:'failed',caption_sequence:1,revision_closed:true})")
    assert page.locator('#asrError').is_visible()
    page.evaluate("handleSubtitle({channel:'original',text:'new speech',stream_id:'restarted',caption_sequence:1,revision_closed:true})")
    assert page.locator('#asrError').is_hidden()


def test_translation_context_replaces_fragment_and_commits_one_sentence(page):
    send(page, 'Texto anterior.', 1)
    page.evaluate('window.firstParagraph = original.history.firstElementChild')
    key='mt-sentence:source:prefix:1'
    metadata={'sentence_key':key,'sentence_closed':False}
    send(page,'En tercer lugar, hay una',2,key=key,partial=True,closed=False,translation_group=metadata)
    text='Y en tercer lugar, hay otra cuestión que es interesante.'
    send(page,text,3,key=key,partial=True,closed=False,translation_group=metadata)
    assert page.locator('#originalSubtitle .caption-history p').all_text_contents()==['Texto anterior.']
    assert page.locator('#originalSubtitle .caption-draft p').all_text_contents()==[text]
    send(page,text,4,key=key,translation_group=dict(metadata,sentence_closed=True))
    send(page,'Una Otra cuestión',5,key=key,translation_group=metadata)
    assert page.locator('#originalSubtitle .caption-history p').all_text_contents()==['Texto anterior.',text]
    assert page.locator('#originalSubtitle .caption-draft').is_hidden()
    assert page.evaluate('original.history.firstElementChild === firstParagraph')


def test_bounded_translation_context_starts_a_paragraph_without_inventing_a_period(page):
    send(page,'Una frase larga sin punto',1,key='mt-sentence:1',
         translation_group={'sentence_key':'mt-sentence:1','sentence_closed':True})
    send(page,'Una continuación con el BSC',2,key='mt-sentence:2',
         translation_group={'sentence_key':'mt-sentence:2','sentence_closed':True})
    assert page.locator('#originalSubtitle .caption-history p').all_text_contents()==[
        'Una frase larga sin punto','Una continuación con el BSC']


def test_closed_revision_becomes_one_paragraph_and_next_starts_another(page):
    send(page, "Esborrany", 1, key="draft-1", closed=False)
    send(page, "Text consolidat.", 2, key="draft-1")
    send(page, "Següent fragment.", 3)
    assert page.locator("#originalSubtitle .caption-history p").all_text_contents() == ["Text consolidat.", "Següent fragment."]
    assert page.locator("#originalSubtitle .caption-draft").is_hidden()
    assert page.locator("#originalSubtitle .caption-history p").nth(1).bounding_box()["y"] > page.locator("#originalSubtitle .caption-history p").nth(0).bounding_box()["y"]


def test_expanded_claudia_timeout_keeps_new_words_in_one_completed_paragraph(page):
    prefix = "Aquí ja començo destacant..."
    full = "Aquí ja començo destacant que la Clàudia Goldin va ser la primera dona"
    send(page, prefix, 1, key="claudia", closed=False)
    send(page, full, 2, key="claudia", partial=True, closed=False)
    assert page.locator("#originalSubtitle .caption-draft p").all_text_contents() == [full]
    send(page, full, 3, key="claudia", chunk_id="expanded-final")
    send(page, full, 4, key="claudia", chunk_id="expanded-final")
    send(page, "en aconseguir una posició permanent a la Universitat de Harvard.", 5)
    assert page.locator("#originalSubtitle .caption-history p").all_text_contents() == [
        full + " en aconseguir una posició permanent a la Universitat de Harvard."
    ]
    assert page.locator("#originalSubtitle .caption-draft").is_hidden()


def test_bounded_window_tail_correction_stays_in_one_draft_until_closed(page):
    first = "I són els coneixements en aquestes dues branques el que fan tan particular la seva recerca, com veurem a continuació."
    corrected = "I són els coneixements en aquestes dues branques el que fan en particular, a la seva recerca, com veurem a continuació."
    send(page, first, 1, key="bounded", closed=False, finalization_reason="bounded_window")
    assert page.locator("#originalSubtitle .caption-history p").count() == 0
    send(page, corrected, 2, key="bounded", closed=False, partial=True)
    send(page, corrected, 3, key="bounded", closed=False)
    assert page.locator("#originalSubtitle .caption-history p").count() == 0
    assert page.locator("#originalSubtitle .caption-draft p").all_text_contents() == [corrected]
    send(page, corrected, 4, key="bounded", closure_reason="confirmed")
    assert page.locator("#originalSubtitle .caption-history p").all_text_contents() == [corrected]
    assert page.locator("#originalSubtitle .caption-draft").is_hidden()


def test_late_partial_and_duplicate_final_cannot_rewrite_history(page):
    send(page, "Primer text.", 2, key="draft-1", chunk_id="final-1")
    send(page, "Parcial antic", 1, partial=True, key="draft-1", closed=False)
    send(page, "Revisió antiga", 3, partial=True, key="draft-1", closed=False)
    send(page, "Missatge duplicat", 4, chunk_id="final-1")
    assert page.locator("#originalSubtitle .caption-history p").all_text_contents() == ["Primer text."]
    assert page.locator("#originalSubtitle .caption-draft").is_hidden()


def test_stream_end_commits_timeout_once_and_resets_cleanly(page):
    send(page, "Últim esborrany", 1, key="draft-1", closed=False, chunk_id="timeout-1")
    send(page, "Últim esborrany", 2, key="draft-1", chunk_id="timeout-1", closure_reason="stream_end")
    assert page.locator("#originalSubtitle .caption-history p").all_text_contents() == ["Últim esborrany"]
    assert page.locator("#originalSubtitle .caption-draft").is_hidden()
    page.evaluate("original.reset()")
    assert page.locator("#originalSubtitle .caption-history p").count() == 0
    assert page.locator("#originalSubtitle .caption-placeholder").inner_text() == "Waiting for speech..."


def test_channels_are_independent_and_text_is_not_html(page):
    send(page, '<img src=x onerror="window.injected=true">', 1)
    page.evaluate("translation.receive({text:'English text.', revision_closed:true, caption_sequence:1, stream_id:'translation'})")
    assert page.locator("#originalSubtitle img").count() == 0
    assert not page.evaluate("Boolean(window.injected)")
    assert page.locator("#translationSubtitle .caption-history p").all_text_contents() == ["English text."]


def test_retention_is_bounded_and_new_stream_can_reuse_chunk_ids(page):
    for sequence in range(1, 25):
        send(page, f"Fragment {sequence}.", sequence)
    assert page.locator("#originalSubtitle .caption-history p").count() <= 10
    assert page.locator("#originalSubtitle .caption-history p").last.inner_text() == "Fragment 24."
    page.evaluate("original.receive({text:'Nova sessió.', caption_sequence:1, chunk_id:'1', stream_id:'new-stream', revision_closed:true})")
    assert page.locator("#originalSubtitle .caption-history p").last.inner_text() == "Nova sessió."
    page.evaluate("original.receive({text:'Old session packet', caption_sequence:30, stream_id:'test-stream', revision_closed:true})")
    assert page.locator("#originalSubtitle .caption-history p").last.inner_text() == "Nova sessió."


@pytest.mark.parametrize("width,height,expected_height", [
    (1440, 1080, 220), (1280, 768, 176.64), (375, 812, 160), (320, 568, 160),
])
def test_responsive_layout_keeps_text_size_and_steady_panel_height(page, width, height, expected_height):
    page.set_viewport_size({"width": width, "height": height})
    before = page.locator("#originalSubtitle").bounding_box()
    assert before["height"] == pytest.approx(expected_height, abs=1)
    player = page.locator(".player-container").bounding_box()
    if width > 600:
        assert player["width"] == pytest.approx(min(width * .85, 1000), abs=1)
    else:
        assert player["width"] == pytest.approx(width - 32, abs=1)
    send(page, "Un fragment consolidat.", 1)
    send(page, "Una hipòtesi que encara està canviant", 2, partial=True, closed=False)
    assert page.locator("#originalSubtitle").bounding_box() == before
    assert page.locator("#originalSubtitle").evaluate("e => getComputedStyle(e).fontSize") == "16px"
    assert page.locator("#originalSubtitle").evaluate("e => getComputedStyle(e).textAlign") == "left"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_draft_badge_is_in_heading_and_hidden_after_consolidation(page):
    heading = page.locator(".subtitle-section").first.locator(".subtitle-heading")
    badge = heading.locator(".caption-draft-label")
    assert badge.is_hidden()
    send(page, "Un esborrany", 1, partial=True, closed=False)
    assert badge.is_visible()
    assert page.locator("#originalSubtitle .caption-draft-label").count() == 0
    assert badge.bounding_box()["y"] < page.locator("#originalSubtitle").bounding_box()["y"]
    send(page, "Text consolidat.", 2)
    assert badge.is_hidden()


def test_taller_panel_can_show_seven_complete_lines(page):
    page.set_viewport_size({"width": 1440, "height": 1080})
    for sequence in range(1, 8):
        send(page, f"Fragment consolidat {sequence}.", sequence)
    assert page.locator("#originalSubtitle .caption-history p").count() == 7
    panel = page.locator("#originalSubtitle").bounding_box()
    last = page.locator("#originalSubtitle .caption-history p").last.bounding_box()
    assert last["y"] + last["height"] <= panel["y"] + panel["height"]


def test_consecutive_sentence_chunks_append_inline_without_rewriting_previous_words(page):
    texts = ["i l'any 2023", "és el torn", "de la Clàudia Goldin", "que per cert és la primera dona"]
    send(page, texts[0], 1, audio_start_s=108.528, audio_end_s=110.708)
    page.evaluate("window.firstSpan = original.history.querySelector('.caption-chunk')")
    for index, text in enumerate(texts[1:], 2):
        send(page, text, index, audio_start_s=108+index, audio_end_s=109+index)
    assert page.locator("#originalSubtitle .caption-history p").all_text_contents() == [' '.join(texts)]
    assert page.locator("#originalSubtitle .caption-chunk").count() == 4
    assert page.evaluate("original.history.querySelector('.caption-chunk') === firstSpan && firstSpan.textContent === \"i l'any 2023\"")
    send(page, "Un esborrany canviant", 5, partial=True, closed=False)
    assert page.locator("#originalSubtitle .caption-history p").inner_text() == ' '.join(texts)
    assert page.locator("#originalSubtitle .caption-draft p").inner_text() == "Un esborrany canviant"


def test_corrected_elinor_chunks_form_one_sentence_paragraph(page):
    first = "La primera va ser l'any 2009, la Elinor Armstrong, que juntament amb l'Oliver Williamson"
    rest = ["va ser la primera dona que guanya el Premi Nobel", "per la seva anàlisi de la governança econòmica."]
    send(page, "La primera va ser l'any 2009, la Elinor Om.", 1, key="elinor", closed=False)
    send(page, first, 2, key="elinor", audio_start_s=72.876, audio_end_s=79.476)
    send(page, rest[0], 3, audio_start_s=79.476, audio_end_s=83.096)
    send(page, rest[1], 4, audio_start_s=83.096, audio_end_s=85.916)
    assert page.locator("#originalSubtitle .caption-history p").all_text_contents() == [' '.join([first, *rest])]
    assert "Elinor Om" not in page.locator("#originalSubtitle").inner_text()


def test_audio_pause_starts_new_paragraph_without_sentence_punctuation(page):
    send(page, "Una primera idea", 1, audio_start_s=0, audio_end_s=1)
    send(page, "i la continuació", 2, audio_start_s=1.1, audio_end_s=2)
    send(page, "Una altra idea", 3, audio_start_s=4.1, audio_end_s=5)
    assert page.locator("#originalSubtitle .caption-history p").all_text_contents() == [
        "Una primera idea i la continuació", "Una altra idea"
    ]


def test_translation_or_network_delay_does_not_split_a_sentence(page):
    send(page, "Una primera part", 1, audio_start_s=0, audio_end_s=1, timestamp=100)
    send(page, "que continua", 2, audio_start_s=1.1, audio_end_s=2, timestamp=1000)
    assert page.locator("#originalSubtitle .caption-history p").all_text_contents() == ["Una primera part que continua"]


@pytest.mark.parametrize("prefix", ["Al final...", "El Dr."])
def test_ellipsis_and_common_abbreviation_do_not_force_sentence_break(page, prefix):
    send(page, prefix, 1)
    send(page, "continua parlant", 2)
    assert page.locator("#originalSubtitle .caption-history p").all_text_contents() == [prefix + " continua parlant"]


def test_long_sentence_uses_a_length_limit_for_paragraphs(page):
    first, second = "a" * 220, "b" * 220
    send(page, first, 1)
    send(page, second, 2)
    assert page.locator("#originalSubtitle .caption-history p").all_text_contents() == [first, second]


def test_new_stream_cannot_continue_previous_stream_paragraph(page):
    send(page, "Text de la primera sessió", 1)
    page.evaluate("original.receive({text:'Text de la nova sessió',caption_sequence:1,chunk_id:'new',stream_id:'new-stream',revision_closed:true})")
    assert page.locator("#originalSubtitle .caption-history p").all_text_contents() == [
        "Text de la primera sessió", "Text de la nova sessió"
    ]


def test_long_group_follows_latest_text_without_forcing_a_reader_back_down(page):
    page.set_viewport_size({"width": 375, "height": 812})
    send(page, "paraula " * 100, 1)
    metrics = page.locator("#originalSubtitle").evaluate("e=>[e.scrollTop,e.scrollHeight,e.clientHeight]")
    assert metrics[1] > metrics[2]
    assert metrics[1] - metrics[2] - metrics[0] < 2
    page.locator("#originalSubtitle").evaluate("e=>e.scrollTop=0")
    send(page, "Un nou esborrany", 2, partial=True, closed=False)
    assert page.locator("#originalSubtitle").evaluate("e=>e.scrollTop") == 0


def test_translation_fallback_badge_survives_draft_closure_and_next_success(page):
    fallback = {"translation_status": "fallback", "translation_fallback": True,
                "text_language": "ca", "translation_error": "request_error"}
    send(page, "El text original.", 1, key="failed", closed=False, **fallback)
    badge = page.locator("#originalSubtitle .caption-fallback-label")
    assert badge.inner_text() == "Translation unavailable · showing original (CA)"
    assert page.locator("#originalSubtitle .caption-draft .caption-fallback").count() == 1
    send(page, "El text original.", 2, key="failed", **fallback)
    assert badge.count() == 1 and badge.is_visible()
    assert page.locator("#originalSubtitle .caption-history .caption-fallback").count() == 1
    send(page, "La siguiente traducción.", 3, translation_status="translated", text_language="es")
    assert page.locator("#originalSubtitle .caption-chunk").last.inner_text() == "La siguiente traducción."
    assert badge.count() == 1
    page.evaluate("original.reset()")
    assert badge.count() == 0


def test_success_and_legacy_captions_have_no_fallback_badge(page):
    send(page, "Barcelona", 1, translation_status="translated", text_language="es")
    send(page, "Legacy text.", 2)
    assert page.locator(".caption-fallback-label").count() == 0


def test_successful_revision_replaces_fallback_draft_warning(page):
    send(page, "Text original.", 1, key="revision", closed=False,
         translation_status="fallback", text_language="ca")
    assert page.locator(".caption-fallback-label").count() == 1
    send(page, "Texto traducido.", 2, key="revision", translation_status="translated", text_language="es")
    assert page.locator(".caption-fallback-label").count() == 0
    assert page.locator("#originalSubtitle .caption-history p").inner_text() == "Texto traducido."
