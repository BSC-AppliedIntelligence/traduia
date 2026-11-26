"""One persistent ASR choice and one loaded model, shared by all broadcasts.

This module deliberately has no GPU/network imports. The backend supplies the
loader and releaser; leases remain active until its inference thread drains.
"""
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid

DEFAULT_MODEL = "Systran/faster-whisper-large-v3"
MODELS = (
    {"id": DEFAULT_MODEL, "label": "Systran Large-v3 (recommended for live transcription)"},
    {"id": "Systran/faster-whisper-small", "label": "Systran Small (lower GPU memory usage)"},
    {"id": "BSC-LT/faster-whisper-large-v3-ca-punctuated-3370h",
     "label": "BSC Large-v3 Catalan — punctuated, 3370h (Catalan-specialized; for comparison)"},
)
MODEL_IDS = frozenset(m["id"] for m in MODELS)


class ModelSelectionError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ModelLease:
    def __init__(self, manager, model, model_id):
        self.manager, self.model, self.model_id = manager, model, model_id
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            if self.manager is not None:
                manager, self.manager = self.manager, None
                self.model = None
                manager.release()


class SharedModelSelection:
    def __init__(self, state_file, default_model=DEFAULT_MODEL, max_sessions=4):
        if default_model not in MODEL_IDS:
            raise ValueError("Unsupported initial ASR model")
        self.path = Path(state_file)
        self.default_model = default_model
        self.service_id = uuid.uuid4().hex
        self.selected = default_model
        self.revision = 0
        self.loaded_id = None
        self.model = None
        self.active = 0
        self.loading = False
        self.last_error = None
        self.max_sessions = max_sessions
        self.lock = threading.Lock()
        self.inference_lock = threading.Lock()
        if self.path.exists():
            if self.path.is_symlink():
                raise ValueError("Model selection state cannot be a symlink")
            state = json.loads(self.path.read_text())
            if (set(state) != {"schema_version", "selected_model", "revision"}
                    or state["schema_version"] != 1 or state["selected_model"] not in MODEL_IDS
                    or type(state["revision"]) is not int or state["revision"] < 0):
                raise ValueError("Invalid persisted model selection; repair it before startup")
            self.selected, self.revision = state["selected_model"], state["revision"]

    def _status(self):
        return {"enabled": True, "service_id": self.service_id, "default_model": self.default_model,
                "models": [dict(m) for m in MODELS], "selected_model": self.selected,
                "loaded_model": self.loaded_id, "selection_revision": self.revision,
                "active_sessions": self.active, "loading": self.loading,
                "pending": self.selected != self.loaded_id, "last_error": self.last_error}

    def status(self):
        with self.lock:
            return self._status()

    def select(self, model_id, expected_revision):
        with self.lock:
            if not isinstance(model_id, str) or model_id not in MODEL_IDS:
                raise ModelSelectionError("invalid_model", "Choose one of the supported ASR models.")
            if type(expected_revision) is not int or expected_revision != self.revision:
                raise ModelSelectionError("selection_conflict", "The shared selection changed. Refresh and try again.")
            if self.loading:
                raise ModelSelectionError("model_busy", "A model is loading. Wait before changing the selection.")
            if model_id != self.selected:
                # Persist first: failed writes must never change the active desired state.
                temporary = None
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent,
                                                     prefix=".asr-selection-", delete=False) as target:
                        temporary = Path(target.name)
                        os.chmod(temporary, 0o600)
                        json.dump({"schema_version": 1, "selected_model": model_id,
                                   "revision": self.revision + 1}, target)
                        target.write("\n")
                        target.flush()
                        os.fsync(target.fileno())
                    os.replace(temporary, self.path)
                except OSError:
                    raise ModelSelectionError("selection_write_failed", "Could not save the shared ASR selection.") from None
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                self.selected, self.revision = model_id, self.revision + 1
                self.last_error = None
            return self._status()

    def acquire(self, loader, releaser):
        with self.lock:
            if self.loading:
                raise ModelSelectionError("model_busy", "The selected ASR model is loading. Retry when ready.")
            if self.active and self.selected != self.loaded_id:
                raise ModelSelectionError("model_busy", "A model change is pending. Stop the existing broadcasts before starting another.")
            if self.active >= self.max_sessions:
                raise ModelSelectionError("model_busy", "All ASR broadcast slots are occupied.")
            if self.model is not None and self.loaded_id == self.selected:
                self.active += 1
                return ModelLease(self, self.model, self.loaded_id)
            if self.active:
                raise ModelSelectionError("model_busy", "Previous ASR work is still draining.")
            target = self.selected
            previous, self.model = self.model, None
            self.loaded_id = None
            self.loading = True
            self.last_error = None
        try:
            # No manager lock during I/O: status remains readable. Select and
            # other starts are rejected while loading, including model release.
            if previous is not None:
                releaser(previous)
                previous = None
            model = loader(target)
            if model is None:
                raise RuntimeError("Loader returned no model")
        except Exception:
            with self.lock:
                self.loading = False
                self.last_error = "The selected model could not be loaded. Retry Start or select another model."
            raise ModelSelectionError("model_load_failed", self.last_error) from None
        with self.lock:
            self.model, self.loaded_id = model, target
            self.loading = False
            self.active += 1
            return ModelLease(self, model, target)

    def release(self):
        with self.lock:
            if self.active <= 0:
                raise RuntimeError("ASR model lease released twice")
            self.active -= 1

    def control(self, request):
        """Short-lived control messages never load a model or acquire a slot."""
        try:
            operation = request.get("operation")
            if operation == "status":
                state = self.status()
            elif operation == "select":
                state = self.select(request.get("model"), request.get("expected_revision"))
            else:
                raise ModelSelectionError("invalid_operation", "Unsupported ASR model operation.")
            return {"type": "asr_model_control", "ok": True, "state": state}
        except ModelSelectionError as error:
            return {"type": "asr_model_control", "ok": False,
                    "error": error.code, "message": str(error)}
