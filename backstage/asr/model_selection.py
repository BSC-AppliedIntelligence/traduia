"""Internal WhisperLive model-control client; configuration stays with ASR."""
import asyncio
import json

import websockets


class ModelControlError(Exception):
    def __init__(self, code, message, status=503):
        super().__init__(message)
        self.code, self.status = code, status


async def model_request(config, operation="status", *, model=None, revision=None):
    host, port = config.get("host", "").strip(), int(config.get("port", 0) or 0)
    if not host or not 1 <= port <= 65535:
        raise ModelControlError("endpoint_missing", "Save the WhisperLive host and port first.")
    request = {"type": "asr_model_control", "operation": operation}
    if operation == "select":
        request.update(model=model, expected_revision=revision)
    try:
        async with websockets.connect(f"ws://{host}:{port}", open_timeout=5,
                                      close_timeout=2, ping_interval=None) as ws:
            await ws.send(json.dumps(request))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 8))
        if reply.get("type") != "asr_model_control":
            raise ModelControlError("unsupported_server", "This ASR server does not support shared model selection.")
        if not reply.get("ok"):
            code = reply.get("error", "control_failed")
            messages = {
                "invalid_model": "Choose one of the supported ASR models.",
                "selection_conflict": "The shared selection changed. Refresh and try again.",
                "model_busy": "A model is loading. Wait before changing the selection.",
                "selection_disabled": "Shared model selection is not enabled on this ASR server.",
                "selection_write_failed": "The ASR server could not save the model selection.",
            }
            raise ModelControlError(code, messages.get(code, "ASR model operation failed."),
                                    409 if code in {"selection_conflict", "model_busy"} else
                                    422 if code == "invalid_model" else 503)
        state = reply["state"]
        if not state.get("enabled") or not isinstance(state.get("models"), list):
            raise ValueError("Invalid model status")
        return state
    except ModelControlError:
        raise
    except Exception:
        # Never pass connection strings, server errors or configuration values
        # into the UI; old fixed-model servers remain usable for transcription.
        raise ModelControlError("control_unavailable", "Shared model status is unavailable. Check that the ASR server supports this feature.") from None
