# Shared ASR model selection

Each event's **Endpoints** tab can manage the model shared by all events using
its saved WhisperLive host/port. This requires a WhisperLive deployment with
shared model selection enabled; older fixed-model servers show status as
unavailable and leave model controls disabled. Host/port/translator settings
remain editable separately with **Save Endpoints**.
Backstage must also enable `WHISPER_MODEL_SELECTION_REQUIRED`; otherwise shared
model management is refused without contacting ASR. Deployments enable
both sides together so broadcast-level guards cannot be accidentally omitted.

The three supported choices are:

- Systran Large-v3 (recommended for live transcription), the initial default.
- Systran Small (lower GPU memory usage).
- BSC Large-v3 Catalan — punctuated, 3370h (Catalan-specialized; for comparison).

Choose a model and click **Save shared model**, or use the bottom
**Save Endpoints and Model** button to save endpoint settings and a pending
model choice together. The form reports when endpoints were saved but the
model failed; a retryable model draft and its error stay visible. When changing
the ASR host/port, save that endpoint first, then choose its model; a draft for
the previous service is never carried across. Saving does not reload a model or
alter an ongoing broadcast. Other event pages see the shared selection when
refreshed or polled. If a different model is pending, new broadcast Starts are
refused until the old ASR broadcasts and inference work have drained. The next
Start downloads/loads the selected model; audio starts only after readiness.
Failed loading leaves the broadcast unstarted and can be retried or corrected
by choosing another model. No model is silently substituted.

The panel shows the running and saved/pending models, loading state and failures.
Saving an older version after another operator changed it returns a conflict;
refresh and review the latest selection. Save endpoint address changes first;
the model control always talks to the event's saved endpoint, not an unsaved
host input. A model draft is discarded when changing to another saved endpoint.
Endpoint saves and shared-model saves remain separate server operations; the
combined form reports partial failures explicitly and never claims a model
was saved after an endpoint-only success.

## Internal protocol and startup

`GET /api/event/{code}/asr-model` reads the saved endpoint then requests status.
`POST` to the same route accepts `model` and `selection_revision`, returning the
new shared status. The selection lives with WhisperLive, not in each event's
Supabase JSON. The legacy `whisper_config.model` remains for compatibility with
older clients but does not override a managed server's shared model.

Backstage uses short-lived JSON model-control messages on the existing internal
WhisperLive WebSocket port. No Docker socket or additional host port is needed.
The ASR server remains the authority for validation, persistence, admission and
actual model identity. These controls use the existing event-management access
boundary; they do not introduce a separate authentication/role system.

Before creating broadcast tracks, shared-ASR startup opens one actual ASR
connection and waits for the model to be ready. That connection is handed to
the event's source agent, avoiding a second model reservation. Failed or
cancelled preparation closes it. Failed LiveKit/agent setup also cleans it up.
Concurrent Start clicks and Stop for the same event are serialized, so a Stop
cannot tear down a partially constructed broadcast.

Deployments set `WHISPER_MODEL_SELECTION_REQUIRED=true`: model-control
status is then required before starting/reconnecting ASR. Backstage also checks
logical live broadcasts, because an audio socket can end before the operator
presses Stop. This prevents a pending switch during that gap. Service identity
from the handshake recognizes another hostname for the same ASR process, with
endpoint matching as a restart fallback. A source agent pins its first reported
model and refuses a reconnect reporting a different one, requiring Stop/Start.
The current feature targets one Backstage process per ASR service, as in the
deployment; multiple Backstage replicas would need distributed broadcast ownership.

`WHISPER_MODEL_READY_TIMEOUT_S` controls the bounded startup wait (default and
maximum 1800 seconds, minimum 20). The Endpoints status poll remains responsive
during loading. A long initial download is distinct from normal stream latency.

Verify GPU model switching on a real GPU host;
a deployment with a fixed ASR model is not affected.
