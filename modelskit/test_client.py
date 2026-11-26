"""
AINA Translator — Test Client
==============================
Script de prueba para validar el servicio de traducción.

Uso:
    python test_client.py                              # usa localhost:8000
    python test_client.py https://POD-8000.proxy.runpod.net   # usa URL custom
"""

import sys
import time
import requests

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
BASE_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "https://f53vojs1lr4km2-8000.proxy.runpod.net"
TRANSLATE_URL = f"{BASE_URL}/translate"
HEALTH_URL = f"{BASE_URL}/health"

PASS = "✅"
FAIL = "❌"
results: list[tuple[str, bool, str]] = []


DEBUG = True  # Set to False once the service works

def log(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    icon = PASS if ok else FAIL
    print(f"  {icon} {name}" + (f"  ({detail})" if detail else ""))


def debug_response(r):
    """Print raw response info for troubleshooting."""
    if DEBUG:
        print(f"  📡 HTTP {r.status_code} | Content-Type: {r.headers.get('content-type', '?')}")
        body = r.text[:500]
        print(f"  📡 Body: {body if body else '(empty)'}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_health():
    """GET /health — el servicio está vivo y los modelos cargados."""
    print("\n── Health Check ──")
    r = requests.get(HEALTH_URL, timeout=10)
    debug_response(r)
    data = r.json()

    log("status 200", r.status_code == 200)
    log("status ok", data.get("status") == "ok")
    log("device present", data.get("device") in ("cuda", "cpu"), f"device={data.get('device')}")
    log("models loaded", len(data.get("models_loaded", [])) == 6, f"{data.get('models_loaded')}")


def test_single_target():
    """POST /translate — un solo idioma."""
    print("\n── Single Target (en) ──")
    r = requests.post(TRANSLATE_URL, json={
        "text": "Bon dia a tothom",
        "targets": ["en"],
    }, timeout=30)
    debug_response(r)
    data = r.json()

    log("status 200", r.status_code == 200)
    log("en present", "en" in data)
    log("en is string", isinstance(data.get("en"), str) and len(data["en"]) > 0, f'"{data.get("en")}"')
    log("no extra keys", list(data.keys()) == ["en"], f"keys={list(data.keys())}")


def test_multiple_targets():
    """POST /translate — varios idiomas en paralelo."""
    print("\n── Multiple Targets (en, es, fr) ──")
    t0 = time.time()
    r = requests.post(TRANSLATE_URL, json={
        "text": "La intel·ligència artificial està transformant el món",
        "targets": ["en", "es", "fr"],
    }, timeout=60)
    elapsed = time.time() - t0
    debug_response(r)
    data = r.json()

    log("status 200", r.status_code == 200)
    log("3 keys returned", len(data) == 3, f"keys={list(data.keys())}")
    for lang in ("en", "es", "fr"):
        val = data.get(lang)
        log(f"{lang} non-empty string", isinstance(val, str) and len(val) > 0, f'"{val}"')
    log(f"latency", True, f"{elapsed:.2f}s")


def test_all_targets():
    """POST /translate — los 6 idiomas."""
    print("\n── All 6 Targets ──")
    all_langs = ["en", "es", "fr", "pt", "it", "de"]
    t0 = time.time()
    r = requests.post(TRANSLATE_URL, json={
        "text": "Avui fa bon temps per passejar",
        "targets": all_langs,
    }, timeout=120)
    elapsed = time.time() - t0
    debug_response(r)
    data = r.json()

    log("status 200", r.status_code == 200)
    log("6 keys returned", len(data) == 6)
    for lang in all_langs:
        val = data.get(lang)
        log(f"{lang} ok", isinstance(val, str) and len(val) > 0, f'"{val}"')
    log(f"latency", True, f"{elapsed:.2f}s")


def test_unknown_target():
    """POST /translate — un idioma no soportado devuelve null."""
    print("\n── Unknown Target (ja) ──")
    r = requests.post(TRANSLATE_URL, json={
        "text": "Hola",
        "targets": ["en", "ja"],
    }, timeout=30)
    debug_response(r)
    data = r.json()

    log("status 200", r.status_code == 200)
    log("en is string", isinstance(data.get("en"), str))
    log("ja is null", data.get("ja") is None, f"ja={data.get('ja')}")


def test_beam_search():
    """POST /translate — beam_size > 1 funciona."""
    print("\n── Beam Search (beam_size=4) ──")
    r = requests.post(TRANSLATE_URL, json={
        "text": "El president va anunciar noves mesures econòmiques",
        "targets": ["en"],
        "beam_size": 4,
    }, timeout=60)
    debug_response(r)
    data = r.json()

    log("status 200", r.status_code == 200)
    log("en present", isinstance(data.get("en"), str) and len(data["en"]) > 0, f'"{data.get("en")}"')


def test_max_new_tokens():
    """POST /translate — max_decoding_length custom."""
    print("\n── Custom max_decoding_length (32) ──")
    r = requests.post(TRANSLATE_URL, json={
        "text": "Bon dia",
        "targets": ["es"],
        "max_decoding_length": 32,
    }, timeout=30)
    debug_response(r)
    data = r.json()

    log("status 200", r.status_code == 200)
    log("es present", isinstance(data.get("es"), str), f'"{data.get("es")}"')


def test_empty_text_rejected():
    """POST /translate — texto vacío debe dar 422."""
    print("\n── Validation: Empty Text ──")
    r = requests.post(TRANSLATE_URL, json={
        "text": "",
        "targets": ["en"],
    }, timeout=10)
    debug_response(r)

    log("status 422", r.status_code == 422)


def test_missing_targets_rejected():
    """POST /translate — sin campo targets debe dar 422."""
    print("\n── Validation: Missing Targets ──")
    r = requests.post(TRANSLATE_URL, json={
        "text": "Hola",
    }, timeout=10)
    debug_response(r)

    log("status 422", r.status_code == 422)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    print(f"🔗 Testing AINA Translator at: {BASE_URL}")

    # Wait for service to be ready (models can take minutes to load)
    print("\n⏳ Waiting for service to be ready...")
    max_wait = 600  # 10 minutes max
    interval = 10
    elapsed = 0
    ready = False

    while elapsed < max_wait:
        try:
            r = requests.get(HEALTH_URL, timeout=10)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
                print(f"  ✅ Service ready! (waited {elapsed}s)")
                ready = True
                break
            else:
                print(f"  ⏳ HTTP {r.status_code} — not ready yet ({elapsed}s elapsed)")
        except requests.ConnectionError:
            print(f"  ⏳ Connection refused — not ready yet ({elapsed}s elapsed)")
        except requests.Timeout:
            print(f"  ⏳ Timeout — not ready yet ({elapsed}s elapsed)")

        time.sleep(interval)
        elapsed += interval

    if not ready:
        print(f"\n  ❌ Service not ready after {max_wait}s. Check RunPod logs.")
        print("     RunPod → Pod → Logs (or Connect → Terminal → 'curl localhost:8000/health')")
        sys.exit(1)

    tests = [
        test_health,
        test_single_target,
        test_multiple_targets,
        test_all_targets,
        test_unknown_target,
        test_beam_search,
        test_max_new_tokens,
        test_empty_text_rejected,
        test_missing_targets_rejected,
    ]

    for test_fn in tests:
        try:
            test_fn()
        except requests.ConnectionError:
            print(f"\n  {FAIL} {test_fn.__name__}: Connection refused — is the server running?")
            results.append((test_fn.__name__, False, "connection refused"))
        except Exception as e:
            print(f"\n  {FAIL} {test_fn.__name__}: {e}")
            results.append((test_fn.__name__, False, str(e)))

    # Resumen
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    failed = total - passed

    print("\n" + "═" * 50)
    print(f"  Results: {passed}/{total} passed" + (f", {failed} failed" if failed else ""))
    print("═" * 50)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
