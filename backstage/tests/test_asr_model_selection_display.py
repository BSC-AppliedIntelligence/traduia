"""Real Chromium checks of the Endpoints model controls with fake service I/O."""
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
ROOT=Path(__file__).resolve().parents[1]
DEFAULT='Systran/faster-whisper-large-v3'
SMALL='Systran/faster-whisper-small'
BSC='BSC-LT/faster-whisper-large-v3-ca-punctuated-3370h'
MODELS=[{'id':DEFAULT,'label':'Systran Large-v3 (recommended for live transcription)'},
        {'id':SMALL,'label':'Systran Small (lower GPU memory usage)'},
        {'id':BSC,'label':'BSC Large-v3 Catalan — punctuated, 3370h (Catalan-specialized; for comparison)'}]


@pytest.fixture
def panel():
    state={'enabled':True,'models':MODELS,'default_model':DEFAULT,'selected_model':DEFAULT,
           'loaded_model':DEFAULT,'selection_revision':0,'active_sessions':2,
           'pending':False,'loading':False,'last_error':None,'endpoint':{'host':'whisperlive','port':9090}}
    template=(ROOT/'templates/event.html').read_text()
    form=template[template.index('<form id="endpointsForm">'):]
    form=form[:form.index('</form>')+7]
    with playwright.sync_playwright() as tools:
        browser=tools.chromium.launch(headless=True)
        context=browser.new_context()
        def route(request):
            if '/asr-model' in request.request.url:
                if state.get('failure'):
                    request.fulfill(status=503, json={'detail':state['failure']})
                    return
                if request.request.method=='POST':
                    if state.get('save_failure'):
                        request.fulfill(status=503, json={'detail':state['save_failure']})
                        return
                    body=request.request.post_data_json
                    if body['selection_revision']!=state['selection_revision']:
                        request.fulfill(status=409,json={'detail':'The shared selection changed. Refresh and try again.'})
                        return
                    state.update(selected_model=body['model'],selection_revision=state['selection_revision']+1,
                                 pending=body['model']!=state['loaded_model'])
                request.fulfill(json=state)
            elif request.request.url.endswith('/endpoints'):
                if state.get('endpoint_failure'):
                    request.fulfill(status=503,json={'detail':'Endpoint save failed'})
                    return
                config=request.request.post_data_json
                state['endpoint']={'host':config['host'],'port':config['port']}
                request.fulfill(json={'success':True,'config':config})
            elif request.request.url=='http://panel.test/':
                request.fulfill(content_type='text/html',body=form)
            else:
                request.abort()
        context.route('**/*',route)
        def new_page(event='A87729'):
            page=context.new_page()
            page.goto('http://panel.test/')
            page.fill('#whisperHost','whisperlive')
            page.fill('#whisperPort','9090')
            page.add_script_tag(content=(ROOT/'static/js/asr-model-selection.js').read_text())
            page.evaluate('event => { window.panel = new AsrModelSelection({eventCode:event}); clearInterval(panel.timer); }',event)
            page.evaluate('() => panel.refresh()')
            # Exercise the actual template submit handler, not a copied stand-in.
            start=template.index("        document.getElementById('endpointsForm').addEventListener('submit'")
            end=template.index('        // Load endpoints on init',start)
            page.add_script_tag(content='const sharedModelPanel=window.panel;\n'+template[start:end])
            return page
        yield state,new_page
        browser.close()


def test_exact_choices_and_save_from_one_event_is_shared_with_another(panel):
    state,new_page=panel
    first=new_page()
    assert first.locator('#whisperModel option').all_text_contents()==[m['label'] for m in MODELS]
    assert first.input_value('#whisperModel')==DEFAULT
    first.select_option('#whisperModel',SMALL)
    first.click('#saveAsrModel')
    first.wait_for_function('panel.state.selected_model.includes("small") && !panel.saving')
    assert 'existing broadcasts must finish' in first.inner_text('#asrModelStatus')
    assert 'Systran Large-v3' in first.inner_text('#asrRunningModel')
    second=new_page('OTHER')
    assert second.input_value('#whisperModel')==SMALL
    assert state['loaded_model']==DEFAULT


def test_unsaved_endpoint_changes_disable_shared_model_save(panel):
    _,new_page=panel
    page=new_page()
    page.select_option('#whisperModel',BSC)
    page.fill('#whisperHost','another-server')
    assert page.is_disabled('#saveAsrModel')
    assert 'Save the endpoint host and port first' in page.inner_text('#asrModelStatus')


def test_poll_preserves_unsaved_choice_but_stale_save_is_refused(panel):
    state,new_page=panel
    page=new_page()
    page.select_option('#whisperModel',SMALL)
    state.update(selected_model=BSC,selection_revision=1,pending=True)
    page.evaluate('() => panel.refresh()')
    assert page.input_value('#whisperModel')==SMALL
    page.click('#saveAsrModel')
    page.wait_for_function('!panel.saving && document.getElementById("asrModelStatus").textContent.includes("Refresh")')
    assert state['selected_model']==BSC
    assert page.input_value('#whisperModel')==BSC


def test_loading_and_load_failure_are_explicit(panel):
    state,new_page=panel
    state.update(loading=True,loaded_model=None,pending=True)
    page=new_page()
    assert page.is_disabled('#whisperModel')
    assert 'Downloading/loading' in page.inner_text('#asrModelStatus')
    state.update(loading=False,last_error='Could not load model. Retry Start or select another model.')
    page.evaluate('() => panel.refresh()')
    assert page.is_enabled('#whisperModel')
    assert 'Could not load model' in page.inner_text('#asrModelStatus')
    assert 'not loaded' in page.inner_text('#asrRunningModel')


def test_unavailable_model_status_disables_controls_and_explains_the_next_step(panel):
    state, new_page = panel
    state['failure'] = 'Save the WhisperLive host and port first.'
    page = new_page()
    assert page.is_disabled('#whisperModel')
    assert page.is_disabled('#saveAsrModel')
    assert 'Save the WhisperLive host and port first' in page.inner_text('#asrModelStatus')
    assert 'unavailable' in page.inner_text('#asrRunningModel')


def test_bottom_form_save_persists_small_and_reopening_keeps_it(panel):
    state,new_page=panel
    page=new_page()
    page.select_option('#whisperModel',SMALL)
    page.click('#endpointsForm button[type="submit"]')
    page.wait_for_function('document.getElementById("endpointsSaved").textContent === "Endpoints and shared ASR model saved."')
    assert state['selected_model']==SMALL
    assert state['loaded_model']==DEFAULT  # Saving does not change the live model.
    assert new_page().input_value('#whisperModel')==SMALL


def test_bottom_form_save_reports_partial_failure_and_keeps_retryable_choice(panel):
    state,new_page=panel
    page=new_page()
    page.select_option('#whisperModel',SMALL)
    state['save_failure']='The ASR server could not save the model selection.'
    page.click('#endpointsForm button[type="submit"]')
    page.wait_for_function('document.getElementById("endpointsSaved").textContent.includes("model was not saved")')
    assert state['selected_model']==DEFAULT
    assert page.input_value('#whisperModel')==SMALL
    assert page.is_enabled('#saveAsrModel')
    page.evaluate('() => panel.refresh()')
    assert 'could not save' in page.inner_text('#asrModelStatus')
    del state['save_failure']
    page.click('#endpointsForm button[type="submit"]')
    page.wait_for_function('panel.state.selected_model.includes("small") && !panel.formSaving')


def test_endpoint_save_failure_does_not_save_or_discard_model_draft(panel):
    state,new_page=panel
    page=new_page()
    page.select_option('#whisperModel',SMALL)
    state['endpoint_failure']=True
    page.click('#endpointsForm button[type="submit"]')
    page.wait_for_function('document.getElementById("endpointsSaved").textContent.includes("Endpoints could not be saved")')
    assert state['selection_revision']==0
    assert page.input_value('#whisperModel')==SMALL


def test_form_does_not_carry_old_model_draft_to_a_changed_endpoint(panel):
    state,new_page=panel
    page=new_page()
    page.select_option('#whisperModel',SMALL)
    page.fill('#whisperHost','another-server')
    page.click('#endpointsForm button[type="submit"]')
    page.wait_for_function('document.getElementById("endpointsSaved").textContent.includes("Choose and save")')
    assert state['selection_revision']==0
    assert state['selected_model']==DEFAULT
    assert page.input_value('#whisperModel')==DEFAULT
    assert page.is_disabled('#saveAsrModel')


def test_form_stale_selection_reports_partial_save_instead_of_success(panel):
    state,new_page=panel
    page=new_page()
    page.select_option('#whisperModel',SMALL)
    state.update(selected_model=BSC,selection_revision=1,pending=True)
    page.click('#endpointsForm button[type="submit"]')
    page.wait_for_function('document.getElementById("endpointsSaved").textContent.includes("model was not saved")')
    assert state['selected_model']==BSC
    assert page.input_value('#whisperModel')==BSC
    assert 'Refresh' in page.inner_text('#endpointsSaved')
