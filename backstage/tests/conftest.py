"""Keep ASR/caption unit tests independent of local operator credentials."""
import os
from unittest.mock import patch


# main imports dotenv and optionally constructs external-service clients at
# collection time. Never read the developer's populated .env during tests.
_dotenv = patch("dotenv.load_dotenv", return_value=False)
_dotenv.start()
_environment = patch.dict(os.environ, {
    "SUPABASE_URL": "", "SUPABASE_ANON_KEY": "", "SUPABASE_SERVICE_KEY": "",
    "GOOGLE_APPLICATION_CREDENTIALS": "", "GOOGLE_TTS_CREDENTIALS": "",
    "LIVEKIT_API_KEY": "", "LIVEKIT_API_SECRET": "",
    "WHISPER_MODEL_SELECTION_REQUIRED": "false",
})
_environment.start()


def pytest_sessionfinish(session, exitstatus):
    _environment.stop()
    _dotenv.stop()
