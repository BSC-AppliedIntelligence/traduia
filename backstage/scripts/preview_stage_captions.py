#!/usr/bin/env python3
"""Generate a standalone synthetic preview using Stage's actual caption renderer.

No credentials, services, microphones or model downloads are used.
"""
import argparse
import base64
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def preview_html():
    template = (ROOT / "templates/stage.html").read_text()
    styles = (ROOT / "static/css/styles.css").read_text()
    styles += re.search(r"<style>(.*?)</style>", template, re.S).group(1)
    renderer = (ROOT / "static/js/stage-captions.js").read_text()
    # Use the real page structure/layout, but omit its service scripts and
    # injected configuration. Preview controls send synthetic caption events.
    markup = template.split("<body>", 1)[1].split('    <script src=', 1)[0]
    markup = markup.replace('<div id="loadingState">', '<div id="loadingState" class="hidden">')
    markup = markup.replace('<div id="liveState" class="hidden">', '<div id="liveState">')
    markup = markup.replace('id="eventCode">Loading...', 'id="eventCode">A87729 · Preview')
    markup = markup.replace('id="eventTitle">Event', 'id="eventTitle">Caption preview')
    markup = markup.replace('Select a language channel to listen', 'Offline preview · synthetic text')
    markup = markup.replace('id="statusText">Connecting...', 'id="statusText">Preview')
    markup = markup.replace('<!-- Channels will be loaded here -->', '''
        <button class="channel-btn"><span class="channel-flag">🎤</span><span>Original</span></button>
        <button class="channel-btn"><span class="channel-flag">🇪🇸</span><span>Spanish</span></button>
        <button class="channel-btn active"><span class="channel-flag">🇬🇧</span><span>English</span></button>''')
    logo = base64.b64encode((ROOT / "static/img/bscTranslatorLogo.png").read_bytes()).decode()
    markup = markup.replace('/static/img/bscTranslatorLogo.png', 'data:image/png;base64,' + logo)
    return """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Stage captions — local preview</title><style>""" + styles + """
.preview-controls {position:fixed;bottom:8px;right:8px;z-index:5;display:flex;
flex-wrap:wrap;align-items:center;gap:8px;padding:8px;background:var(--bg-panel);
border:1px solid var(--border-subtle);border-radius:8px;max-width:calc(100vw - 16px);font-size:12px;}
#replay,#next {padding:6px 10px;cursor:pointer;}
</style><body>""" + markup + """
<aside class="preview-controls" aria-label="Synthetic preview controls">
<button id="replay">Replay example</button><button id="next">Next step</button>
<span id="step"></span></aside>
<script>""" + renderer + """</script><script>
const original = new StageCaptionPanel(document.getElementById('originalSubtitle'), 'Waiting for speech...');
const translation = new StageCaptionPanel(document.getElementById('translationSubtitle'), 'Listening...');
const examples = [
  ['Benvinguts a la sessió.', 'Welcome to the session.', 'stable', true, ''],
  ['Avui parlarem', null, 'partial', false, ''],
  ['Avui parlarem de traducció.', 'Today we will discuss translation.', 'timeout', false, 'asr-timeout:1'],
  ['Avui parlarem de traducció en', null, 'partial', false, 'asr-timeout:1'],
  ['Avui parlarem de traducció en directe.', 'Today we will discuss live translation.', 'stable', true, 'asr-timeout:1'],
  ['El següent fragment', null, 'partial', false, ''],
  ['El següent fragment comença en un paràgraf nou.', 'The next chunk starts in a new paragraph.', 'stable', true, ''],
];
let index = 0, timer = null;
function next() {
  if (index >= examples.length) {clearInterval(timer); return;}
  const [text, translated, reason, closed, key] = examples[index];
  const data = {text, is_partial:reason === 'partial', revision_key:key,
    finalization_reason:reason, revision_closed:closed, caption_sequence:index + 1,
    stream_id:'synthetic-preview', chunk_id:'preview-' + index};
  original.receive(data);
  if (translated) translation.receive({...data, text:translated});
  document.getElementById('step').textContent = `Step ${index + 1}/${examples.length}: ${reason}${closed ? ' · consolidated' : ' · live draft'}`;
  index++;
}
function replay() {clearInterval(timer); original.reset(); translation.reset(); index = 0; next(); timer = setInterval(next, 1800);}
document.getElementById('replay').onclick = replay;
document.getElementById('next').onclick = () => {clearInterval(timer); next();};
next();
</script></body></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/traduia-stage-captions-preview/index.html"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(preview_html())
    print(args.output)


if __name__ == "__main__":
    main()
