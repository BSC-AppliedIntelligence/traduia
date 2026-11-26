# Developer guide

For people who want to adapt TraduIA: change the code, build their own images, extend the schema, or understand why
the deployment is built the way it is. To simply run the system, use the [quick start](quickstart.md); the
[Swarm reference](../swarm/README.md) describes the stack and its operation.

- [Architecture](#architecture)
- [Repository layout and images](#repository-layout-and-images)
- [Reference images and your own](#reference-images-and-your-own)
- [Developing and testing locally](#developing-and-testing-locally)
- [Database and schema](#database-and-schema)
- [Networking and media](#networking-and-media)
- [Configuration reference](#configuration-reference)
- [Tests](#tests)
- [Releases](#releases)
- [Known issues and backlog](#known-issues-and-backlog)

## Architecture

```text
speaker ─WebRTC─> LiveKit ─> Backstage ─16 kHz audio─> WhisperLive (ASR) ─text─> Backstage ─> ModelsKit (translation)
                                 │                                                      │
                                 │  captions (WebSocket) ──────────────────────────────>│ per language channel
                                 └─ optional TTS (Google) ─> audio tracks ─> LiveKit ─> listeners (Backstage / Stage pages)
Supabase (auth + Postgres) stores users, events, channels; Traefik terminates TLS for everything public.
```

- **Backstage** (FastAPI + Jinja2 templates + vanilla JavaScript, no build step) is the operator application and the
  orchestrator: it owns the LiveKit rooms of an event, feeds the audio to ASR, gets the translations and serves the
  caption pages. Almost all server logic is in `main.py`; events and sessions live in memory (lost on restart).
- **WhisperLive** is the ASR server (a fork of Collabora WhisperLive with stable-prefix captions and shared model selection).
- **ModelsKit** serves the Catalan→ES/EN translation models (CTranslate2).
- **LiveKit** carries the audio; its embedded TURN server relays it for clients that cannot connect directly.
- **Stage** is the retained legacy listener client; the current caption UI is a Backstage page.
- **Supabase** is Postgres + auth (GoTrue) + REST (PostgREST). Only auth and table/RPC access are used.

## Repository layout and images

One repository, one directory per component. Images are published on the GitHub Container Registry under
`ghcr.io/bsc-appliedintelligence/traduia/`.

| Directory | Role | Image | Build file |
| --- | --- | --- | --- |
| `backstage/` | operator app + orchestration; `db/` = database init | `backstage`, `db-init` | `Dockerfile`; `db/Dockerfile` |
| `stage/` | legacy listener client | `stage` | `Dockerfile` |
| `whisperlive/` | ASR server (fork of Collabora WhisperLive, MIT) | `whisperlive` | `docker/Dockerfile.gpu` (amd64 GPU) and variants in `docker/` |
| `modelskit/` | translation service | `modelskit` | `Dockerfile.gpu.ct2-source` (two-stage, CUDA runtime image) |
| `deploy/` | Swarm stack, scripts, release manifests, deployment tests, these documents | – | – |

The Swarm stack runs LiveKit from the upstream `livekit/livekit-server` image, pinned by digest.

The built-in Supabase uses stock images pinned by tag: `supabase/postgres`, `supabase/gotrue`, `postgrest/postgrest`.

## Reference images and your own

**Reference images** are the ones registered per release in [`releases/*.json`](../releases): each service has an
`image` (tag), a `digest` and the `source` it was built from. The Swarm stack defaults to those pins (`tag@digest`), so a
deployment is reproducible, and the images are public: no registry login is needed. The tag is the release date
(`2026.11.26`).

**Your own images** need no YAML editing: every application image of `swarm/stack.yml` has an override variable that
you put in the env file (`traduia.env`):

| Service | Variable |
| --- | --- |
| Backstage | `BSC_BACKSTAGE_IMAGE` |
| Stage | `BSC_STAGE_IMAGE` |
| WhisperLive | `WHISPERLIVE_IMAGE` |
| ModelsKit | `BSC_MODELSKIT_IMAGE` |
| LiveKit | `LIVEKIT_IMAGE` |
| Database init | `SUPABASE_INIT_IMAGE` |

Build with the script every component directory ships (`scripts/docker_build_and_push.sh`; settings by environment
variable: `IMAGE_NAME`, `DOCKERFILE_PATH`, `PLATFORMS`, `PUSH`, `LOAD`). On a single-node swarm `LOAD=true` puts the image
straight into the local Docker, no registry needed:

```bash
# in backstage/ (same pattern in stage/, modelskit/, whisperlive/)
PLATFORMS=linux/amd64 LOAD=true IMAGE_NAME=my/backstage:dev bash scripts/docker_build_and_push.sh
echo 'BSC_BACKSTAGE_IMAGE=my/backstage:dev' >> traduia.env
swarm/scripts/deploy.sh traduia.env
```

| Image | Script defaults to | Notes |
| --- | --- | --- |
| backstage, stage | `Dockerfile`, platforms `linux/amd64,linux/arm64` | set `PLATFORMS=linux/amd64` for `LOAD=true` |
| modelskit | `Dockerfile.gpu.ct2-source`, `linux/amd64,linux/arm64` | compiles CTranslate2 from source: slow first build; the image is about 5 GB |
| WhisperLive | the generic script targets `docker/Dockerfile.gpu.spark.ct2-source` (arm64, Spark); for amd64 use `scripts/docker_build_and_push_amd64.sh` (`docker/Dockerfile.gpu`) | set `IMAGE_NAME` explicitly. The reference image is the amd64 build |
| db-init | `backstage/db/Dockerfile` | ~14 MB; `docker build -f db/Dockerfile -t my/db-init .` in `backstage/` |

For several nodes, push to a registry (`PUSH=true` after `docker login`; any registry works) and use the resulting `tag@digest`
(`docker buildx imagetools inspect <image>` shows the digest). Keep your images compatible with the configuration contract
in the [configuration reference](#configuration-reference): the stack passes plain environment variables and mounts only
the host directories listed in the [Swarm reference](../swarm/README.md#deploying).

## Developing and testing locally

- **Backstage / Stage:** `python -m venv venv && pip install -r requirements.txt`, copy the `.env*.example`, fill in the
  Supabase and LiveKit settings and run `python main.py` (port 8000; Stage uses 8080). The frontend is server-rendered
  Jinja templates plus plain JavaScript under `static/js`: edit and reload, nothing to compile. The server injects
  `SUPABASE_URL` / `SUPABASE_ANON_KEY` into every page (`window.__SUPABASE_URL__`) and `config.js` reads them.
- **A Supabase to develop against:** the quickest is the integration test's recipe (`backstage/scripts/test_db_init.sh`
  starts `supabase/postgres`, GoTrue and PostgREST and runs the init image), or any Supabase project with the migrations
  applied. See [database and schema](#database-and-schema).
- **A whole system with your code:** build the images, set the `*_IMAGE` variables and deploy the stack. One caveat for a
  second copy next to a running deployment on the same Traefik: router and middleware names (`traduia-*`, `supabase-*`)
  are fixed, so use a separate swarm or a separate Traefik, or replace the running stack.

## Database and schema

The schema belongs to the application and lives in `backstage/db/migrations/` as numbered SQL files
(`001_schema.sql` … `005`, `010_admin_bootstrap.sql`). The `db-init` image
([docs](../../backstage/docs/database-init.md)) applies them to a
self-hosted Supabase and sets up what GoTrue and PostgREST need; on Supabase cloud the same files are run by hand in the SQL
editor.

- **Adding a migration:** new file `011_<what>.sql`; it must be idempotent-safe on a database that already has the earlier
  ones; never edit an applied file. Run `scripts/test_db_init.sh` (Docker + Internet) and the pytest tests. Rebuild the init
  image and redeploy: each file is applied once and recorded in `public.app_schema_migrations`.
- **Access model:** all tables use row-level security. A new user gets a `profiles` row from the `handle_new_user` trigger,
  unapproved; an administrator approves users. `ADMIN_EMAILS` fills a private table (`bootstrap_admin_emails`, no API
  grants, read only by the `SECURITY DEFINER` function `is_bootstrap_admin`) so the trigger can create administrators
  directly; no address is hardcoded anywhere.
- **Browser configuration:** the server injects `SUPABASE_URL` / `SUPABASE_ANON_KEY` into every page and `static/js/config.js`
  reads them; it must not hardcode a project.
- **Pitfall:** the policy `profiles_select_authenticated … USING (true)` lets any signed-in user list all profiles; see
  [known issues](#known-issues-and-backlog).

## Networking and media

Why the stack looks the way it does (each point was a real failure, reproduced with `swarm/tests/turn-media-test`):

1. **Clients reach media only through TURN/TLS on 443.** Traefik terminates TLS for `turn.<domain>` (a TCP router, SNI based)
   and forwards plain TCP to LiveKit's embedded TURN (`external_tls`). No UDP port is exposed to the Internet.
2. **The TURN relay lives inside the LiveKit container**, so it reaches LiveKit through the address LiveKit advertises. That
   must be a *public* address (Chromium never pairs a relay with private-only candidates) and something must *listen* there:
   hence `7881/tcp` and `7882/udp` published in host mode, with a host firewall (`swarm/host/livekit-media-fw`) dropping the
   Internet side.
3. **`rtc.ips.includes` = the `docker_gwbridge` subnet.** With `node_ip` set, LiveKit maps all its interfaces to the same
   public candidate and the ICE library keeps only the first; packets arriving on the interface that published-port traffic
   uses were silently ignored. Restricting LiveKit to that interface leaves one socket and one candidate.
4. **Overlay hygiene.** Crash-looping tasks can corrupt the `traefik-public` overlay (symptom: every site 502, ARP failures);
   a middleware shared between services breaks routing when the defining service has no running task, so each service
   defines its own. Both are covered by tests and the troubleshooting table.

The browser test runs two headless Chromium participants with `iceTransportPolicy: relay` and a fake microphone, and checks
that bytes sent equal bytes received through TURN. It covers the LiveKit leg, not ASR.

## Configuration reference

Everything comes from stack environment variables ([`swarm/stack.env.example`](../swarm/stack.env.example)); the stack
file maps them to the services.

| Service | What it receives |
| --- | --- |
| backstage | `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY`; `LIVEKIT_API_KEY`/`SECRET`, `LIVEKIT_URL` (public wss), `LIVEKIT_INTERNAL_URL` (`ws://livekit:7880`); `APP_URL`; `TTS_PROVIDER`, `TTS_VOICE`; `FORWARDED_ALLOW_IPS` (Traefik subnet); the `WHISPER_*` / `BSC_TRANSLATION_*` ASR and caption tuning of the release profile; recordings bind-mounted at `/app/recordings` |
| stage | `SUPABASE_*`, `LIVEKIT_API_KEY`/`SECRET` |
| whisperlive | model-selection state file and `WHISPER_PREFIX_MODELS` (which models use stable-prefix confirmation), cache bind-mounted at `/workspace`, GPU through the NVIDIA runtime |
| modelskit | cache at `/workspace`, GPU through the NVIDIA runtime |
| livekit | `LIVEKIT_CONFIG` (YAML in an environment variable: ports, TURN, keys); no config file or secret |
| supabase-* | `POSTGRES_PASSWORD`, `JWT_SECRET`, `ADMIN_EMAILS`, `SUPABASE_INIT_IMAGE` (optional); GoTrue/PostgREST settings are in the stack file |

Docker's variable substitution has a limit worth knowing when you edit the stack: a variable nested inside another default
(`${A:-x${B}}`) is **not** expanded, and a hyphen in a `${VAR:?message}` is read as a default operator. A unit test guards
the first; always check changes with `docker stack config -c swarm/stack.yml`.

## Tests

| What | Command | Needs |
| --- | --- | --- |
| Deployment (stack, scripts, releases, docs links), in `deploy/` | `python3 -m unittest discover -s tests` | Python 3.12 |
| Stack rendering with your values | `docker stack config -c swarm/stack.yml > /dev/null` | Docker |
| Backstage unit tests | `pytest` | the app's requirements; `node` for the frontend-config tests |
| Database init image | `bash scripts/test_db_init.sh` (in `backstage/`) | Docker, Internet |
| Media through TURN | `swarm/tests/turn-media-test/run.sh relay` | Docker, a deployed stack |
| ASR | event + speaker page. The stable-prefix websocket handshake (`caption_protocols: ["stable_text_prefix_v1"]`, fixed language, bounded audio window) is implemented in `whisper_live/server.py` of `whisperlive/` | a deployed stack |

`tests/test_swarm_stacks.py` locks the lessons of the reference deployment: image defaults equal the registered release,
no env files or literal secrets, a router only uses a middleware its own service defines, only LiveKit publishes ports, the
Supabase block is delimited and removable, no nested variable expansion. `tests/test_swarm_scripts.py` covers the helper
scripts with stub commands.

## Releases

A release registers, per service, the image tag, its digest and the source in `releases/<release>.json`. Run the tests (below) before publishing. To move the Swarm stack to a new release:

1. build the images (tag = release date), push them and record the digests;
2. add the manifest under `releases/` and update the `${..._IMAGE:-…}` defaults in `swarm/stack.yml` (the test compares
   them with the manifest);
3. run the tests and `docker stack config`;
4. deploy to a test environment, run the TURN media test and check microphone → captions → translation with a real browser.

## Known issues and backlog

- **Profile visibility:** any signed-in user can read every profile (name, email, admin flag). Tighten the `profiles` SELECT
  policy (own row, or administrators) after checking which pages rely on it.
- **No SMTP** (sign-ups auto-confirm, no password-reset mail) and **no database backup job**.
- **TTS** needs a Google credentials file that is not provisioned.
- **Verified manually** (operator, from a laptop and a phone on remote networks): the deployed system works, except TTS.
  **Not yet verified:** a reboot of the firewall unit, non-browser WebRTC clients.
