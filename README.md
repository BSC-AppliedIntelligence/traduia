# TraduIA

Real-time speech captioning and translation for live events, developed at the Barcelona Supercomputing Center (BSC-CNS).
A speaker talks into a browser; listeners follow live captions and translations in their own language on their phones.
The system runs on your own GPU server, with open models (Catalan-centred, plus Spanish, English and other languages).

```text
speaker ─WebRTC─> LiveKit ─> Backstage ─> WhisperLive (speech recognition) ─> ModelsKit (translation) ─> captions
```

| Directory | What it is | Image (`ghcr.io/bsc-appliedintelligence/traduia/…`) |
| --- | --- | --- |
| [`backstage/`](backstage) | Operator application and orchestrator (FastAPI), database initialisation in `db/` | `backstage`, `db-init` |
| [`stage/`](stage) | Legacy listener client | `stage` |
| [`whisperlive/`](whisperlive) | Speech-recognition server (fork of Collabora WhisperLive, MIT) | `whisperlive` |
| [`modelskit/`](modelskit) | Translation service (CTranslate2, Projecte AINA models) | `modelskit` |
| [`deploy/`](deploy) | Docker Swarm stack, scripts, release manifests, documentation | – |

## Run it

Follow the **[quick start](deploy/docs/quickstart.md)**: one GPU server, a domain, Traefik in Docker Swarm. The reference
images are public and pinned by digest, so nothing needs to be built.

## Adapt it

Every component has its own `Dockerfile` and README, and the images can be replaced one by one through environment
variables. See the **[developer guide](deploy/docs/developer-guide.md)**.

## Licence

Apache License 2.0 (see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE)); `whisperlive/` keeps its MIT licence. The models are
downloaded at run time from Hugging Face under their own licences, which are listed in the `NOTICE` file.
