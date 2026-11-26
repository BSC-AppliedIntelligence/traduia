"""static/js/config.js must take the Supabase project from the server-injected settings, never hardcode one."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "static" / "js" / "config.js"
TEMPLATES = sorted((ROOT / "templates").glob("*.html"))
NODE = shutil.which("node")


def test_config_js_hardcodes_no_supabase_project():
    text = CONFIG.read_text()
    assert not re.search(r"const SUPABASE_URL = '[^']", text), "hardcoded Supabase URL"
    assert not re.search(r"const SUPABASE_ANON_KEY = '[^']", text), "hardcoded Supabase key"
    assert not re.search(r"eyJ[A-Za-z0-9_-]{20,}\.", text), "JWT literal in config.js"
    assert "window.__SUPABASE_URL__" in text and "window.__SUPABASE_ANON_KEY__" in text


def test_config_js_does_not_require_a_supabase_cloud_hostname():
    assert ".includes('.supabase.co')" not in CONFIG.read_text()


@pytest.mark.parametrize("template", [t for t in TEMPLATES if "config.js" in t.read_text()], ids=lambda t: t.name)
def test_every_page_loading_config_js_injects_the_settings_first(template):
    text = template.read_text()
    inject = text.find("window.__SUPABASE_URL__")
    load = text.find("config.js")
    assert inject != -1 and "window.__SUPABASE_ANON_KEY__" in text, f"{template.name} does not inject the settings"
    assert inject < load, f"{template.name} loads config.js before injecting the settings"


def evaluate(window):
    """Run config.js with stubs for the browser globals; report whether it flagged a configuration error."""
    script = r"""
    const src = require('fs').readFileSync(process.argv[1], 'utf8');
    const win = Object.assign({location: {origin: 'https://app.example'}}, JSON.parse(process.argv[2]));
    const created = [];
    const run = new Function('window', 'document', 'supabase', 'console', src + '\n;return window.SUPABASE_CONFIG_ERROR;');
    const error = run(win, {body: {innerHTML: ''}}, {createClient: (u, k) => { created.push(u); return {}; }}, {log() {}, error() {}});
    console.log(JSON.stringify({error, client: created[0] || null}));
    """
    out = subprocess.run([NODE, "-e", script, str(CONFIG), json.dumps(window)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node not available")
@pytest.mark.parametrize("url", ["https://supabase.example.org", "https://abc.supabase.co", "http://localhost:54321"])
def test_valid_injected_settings_create_the_client(url):
    assert evaluate({"__SUPABASE_URL__": url, "__SUPABASE_ANON_KEY__": "eyJabc"}) == {"error": False, "client": url}


@pytest.mark.skipif(NODE is None, reason="node not available")
@pytest.mark.parametrize("window", [
    {},
    {"__SUPABASE_URL__": "", "__SUPABASE_ANON_KEY__": ""},
    {"__SUPABASE_URL__": "not a url", "__SUPABASE_ANON_KEY__": "eyJabc"},
    {"__SUPABASE_URL__": "https://x.example", "__SUPABASE_ANON_KEY__": "abc"},
])
def test_missing_or_malformed_settings_show_the_configuration_error(window):
    assert evaluate(window) == {"error": True, "client": None}
