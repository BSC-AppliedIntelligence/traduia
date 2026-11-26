#!/usr/bin/env bash
# Browser test of the LiveKit media path. Two participants (publisher with a fake microphone, subscriber)
# in headless Chromium, with ICE forced to relay-only ("relay") so ALL media must go through TURN/TLS.
#
# Usage:  TRADUIA_DOMAIN=example.org LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=... ./run.sh [relay|all]
# Run it on the swarm node or any machine with Docker; the Playwright image is pulled on first use.
# Success = both connections "ok", selected ICE pair succeeded, subscriber bytesReceived ~ publisher bytesSent.
set -euo pipefail
POLICY="${1:-relay}"
: "${LIVEKIT_API_KEY:?}" "${LIVEKIT_API_SECRET:?}"
LK_URL="${LK_URL:-wss://livekit.${TRADUIA_DOMAIN:?TRADUIA_DOMAIN (or LK_URL) required}}"
export PAGE_URL="${PAGE_URL:-https://${TRADUIA_DOMAIN:-localhost}/login}"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"; cp "$HERE/test.js" "$WORK/test.js"; trap 'docker run --rm -v "$WORK":/w alpine rm -rf /w/node_modules >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT
eval "$(python3 - <<'PY'
import os,json,hmac,hashlib,base64,time
b=lambda x:base64.urlsafe_b64encode(x).rstrip(b"=").decode()
def tok(identity,pub):
    n=int(time.time()); h=b(json.dumps({"alg":"HS256","typ":"JWT"}).encode())
    p=b(json.dumps({"iss":os.environ["LIVEKIT_API_KEY"],"sub":identity,"nbf":n,"exp":n+900,"video":{"room":"turn-media-test","roomJoin":True,"canPublish":pub,"canSubscribe":True}}).encode())
    return f"{h}.{p}."+b(hmac.new(os.environ["LIVEKIT_API_SECRET"].encode(),f"{h}.{p}".encode(),hashlib.sha256).digest())
print(f"export TOKEN_A={tok('tester-pub',True)} TOKEN_B={tok('tester-sub',False)}")
PY
)"
docker run --rm --network host --ipc host --init -v "$WORK":/work -w /work -e TOKEN_A -e TOKEN_B -e POLICY="$POLICY" -e LK_URL="$LK_URL" -e PAGE_URL \
  mcr.microsoft.com/playwright:v1.65.1-noble bash -c "npm i --silent --no-audit --no-fund playwright@1.65.1 livekit-client@2.24.1 >/dev/null 2>&1 && node test.js" 2>&1 | tail -400 | python3 "$HERE/summarize.py"
