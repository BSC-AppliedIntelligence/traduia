# TraduIA Backstage

Backstage is the operator application and orchestrator of TraduIA, a real-time speech translation platform for live
events. A speaker talks (typically in Catalan); Backstage captures the audio from LiveKit, sends it to speech recognition
(WhisperLive), gets translations from the translation service (ModelsKit), serves live captions in several languages and
can synthesise the translated speech (Google Cloud TTS, optional) and publish it back as audio tracks.

```text
speaker ─WebRTC─> LiveKit ─> Backstage ─> WhisperLive (ASR) ─> ModelsKit (translation) ─> captions / TTS ─> listeners
```

It is a FastAPI application with server-rendered Jinja2 templates and plain JavaScript (no build step). Users, events and
channels live in Supabase (auth + Postgres). Almost all server logic is in `main.py`; events and live sessions are kept
in memory, so a restart ends running sessions.

> A Spanish version of the detailed documentation (project structure, REST API, environment variables, user flow) is in
> [`README.es.md`](README.es.md).

## Run it

The recommended way to run the whole system (Backstage, ASR, translation, LiveKit and a self-hosted Supabase) is the
all-in-one deployment in [`../deploy`](../deploy/docs/quickstart.md), which uses the published image
`ghcr.io/bsc-appliedintelligence/traduia/backstage`.

For development:

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.backstage.example .env       # fill in Supabase and LiveKit settings
python main.py                       # http://localhost:8000
```

Frontend settings are injected by the server: set `SUPABASE_URL` and `SUPABASE_ANON_KEY` in `.env`; nothing is hardcoded in
`static/js/config.js`. Per-event ASR and translator endpoints are configured in the web UI, not globally.

## Database

The schema is the numbered migrations in [`db/migrations`](db/migrations). A one-shot image
(`ghcr.io/bsc-appliedintelligence/traduia/db-init`) applies them to a self-hosted Supabase, sets the service-role passwords
and creates the administrators listed in `ADMIN_EMAILS`: see [`docs/database-init.md`](docs/database-init.md). With Supabase
cloud, run the same files by hand in the SQL editor. `sql/` keeps two manual helper scripts.

## Tests

```bash
pip install pytest                   # plus requirements.txt
pytest                               # node is needed for the frontend-config tests
bash scripts/test_db_init.sh         # Docker integration test of the database init image (needs Docker and Internet)
```

`scripts/preview_stage_captions.py` renders the caption page offline for visual checks. More design notes are in
[`docs/`](docs): ASR model selection, translation grouping and the stage caption protocol.

## Docker

```bash
docker build -t traduia-backstage .
docker run -p 8000:8000 --env-file .env traduia-backstage
```

`scripts/docker_build_and_push.sh` builds (and optionally pushes) with buildx; configure it with `IMAGE_NAME`,
`DOCKERFILE_PATH`, `PLATFORMS`, `PUSH` and `LOAD`.

## Licence

Apache-2.0, see the `LICENSE` and `NOTICE` files at the repository root.
