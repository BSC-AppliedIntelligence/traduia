# TraduIA Stage

Stage is the legacy **listener** client of TraduIA: a small FastAPI application with a static frontend where the audience
of an event picks a language and follows the translation as live captions and audio. It reads events and channels from
Supabase and joins the event's LiveKit room. The current caption interface is served by Backstage; Stage is kept for
deployments that still use it.

> A Catalan version of the detailed documentation (screens, API endpoints, LiveKit data-channel topics) is in
> [`README.ca.md`](README.ca.md).

## Run it

Use the all-in-one deployment in [`../deploy`](../deploy/docs/quickstart.md), which runs the published image
`ghcr.io/bsc-appliedintelligence/traduia/stage`. For development:

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.stage.example .env           # Supabase and LiveKit settings (same project as Backstage)
python main.py                       # http://localhost:8080 (port configurable with PORT)
```

## Docker

```bash
docker build -t traduia-stage .
docker run -p 8080:8080 --env-file .env traduia-stage
```

`scripts/docker_build_and_push.sh` builds (and optionally pushes) with buildx; configure it with `IMAGE_NAME`,
`DOCKERFILE_PATH`, `PLATFORMS`, `PUSH` and `LOAD`.

## Licence

Apache-2.0, see the `LICENSE` and `NOTICE` files at the repository root.
