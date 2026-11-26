# TraduIA on Docker Swarm: reference

Reference for the all-in-one Swarm deployment. **To deploy, start with the [quick start](../docs/quickstart.md);** to
adapt the system or build your own images, see the [developer guide](../docs/developer-guide.md). This page explains
what is in the stack and how to operate it.

| Path | What it is |
| --- | --- |
| [`stack.yml`](stack.yml), [`stack.env.example`](stack.env.example) | The stack: Backstage, Stage, WhisperLive, ModelsKit, LiveKit and (removable) a self-hosted Supabase |
| [`scripts/`](scripts) | `init-env.py` (create the env file), `deploy.sh` (host setup + deploy + wait), `generate-supabase-secrets.py` |
| [`host/livekit-media-fw/`](host/livekit-media-fw) | Host firewall that keeps the LiveKit media ports off the Internet (TURN-only media) |
| [`tests/turn-media-test/`](tests/turn-media-test) | Browser test that media flows through TURN to LiveKit |

All settings are **stack environment variables** (an env file, or the Portainer stack environment). There are no env
files inside the stack and no Docker secrets; remember that Docker administrators can read service environments.

## Status and validation

Reference deployment: one GPU host (Ubuntu 24.04, Docker 29, single-node Swarm, NVIDIA default runtime, Traefik v3
with a Let's Encrypt resolver), validated 2026-11-17/25:

- the stack renders (`docker stack config`) with the live values and runs there as one all-in-one stack, including the
  move of a database volume from a former separate Supabase stack (all migrations reported as already applied, existing
  administrators log in);
- sign-up/login/row-level security/admin bootstrap through the public API; WhisperLive `large-v3` on the GPU through its
  websocket protocol;
- with ICE forced to relay-only, a headless browser publishes a (fake) microphone and a second participant receives it
  through TURN/TLS on 443 ([`tests/turn-media-test`](tests/turn-media-test));
- `deploy.sh` and `init-env.py` are covered by unit tests with stub commands and were run against real Docker with a
  throwaway stack;
- the operator tested the deployed system manually from remote systems (a laptop and a phone): it works correctly, except
  text-to-speech, which is unavailable because no Google credentials are provisioned.

**Not verified**: a reboot of the firewall unit and non-browser WebRTC clients. After any change, repeat the media test and
a remote test (another network, ideally a phone as well as a laptop) before accepting a deployment.

## Architecture

```text
Internet ──443──> Traefik ──> backstage (8000)      https://<domain>, https://backstage.<domain>
                          ├─> stage (8080)          https://stage.<domain>
                          ├─> livekit (7880, WSS)   https://livekit.<domain>
                          ├─> livekit TURN (5349)   turn.<domain>:443  (TCP router, TLS terminated by Traefik)
                          ├─> supabase-auth (9999)  https://supabase.<domain>/auth/v1   ┐ the removable
                          └─> supabase-rest (3000)  https://supabase.<domain>/rest/v1   ┘ Supabase block
backstage ─ws─> whisperlive (9090), modelskit (8000), livekit (7880)      (private overlay, never published)
supabase-init (one-shot job) ─> supabase-db (Postgres 5432)               (private overlay, never published)
```

Media goes **only through TURN over TLS on 443**. LiveKit also publishes `7881/tcp` and `7882/udp` in host mode, but
only so that the TURN relay inside the LiveKit container can reach LiveKit through the advertised public IP; the
[host firewall](host/livekit-media-fw) drops Internet traffic to them. Direct UDP media from outside is therefore
unavailable by design. The reasons behind this layout are in the
[developer guide](../docs/developer-guide.md#networking-and-media).

## Prerequisites

- One Linux AMD64 node (Swarm manager) with an NVIDIA GPU, driver 580.173.02+, NVIDIA Container Toolkit and Docker's
  **default runtime set to `nvidia`**. Swarm's stack format has no `gpus:` key, so GPU access comes from the default
  runtime; `NVIDIA_VISIBLE_DEVICES` alone does not enable GPUs with `runc`. Do not restart the shared daemon without
  scheduling the impact on existing services.
- An existing Traefik (v3 tested; v2 not tested) in the same Swarm with the Swarm provider (`exposedByDefault=false`),
  entrypoints `web` (:80) and `websecure` (:443), an ACME resolver using the HTTP challenge, and the external overlay
  network `traefik-public`. This repository does not deploy Traefik.
- DNS A records (or a wildcard) to the node's public IP for `<domain>`, `backstage.`, `stage.`, `livekit.`, `turn.` and
  `supabase.<domain>` (the last one only with the built-in Supabase); ports 80/443 reachable for the ACME challenge.
- Internet access to `ghcr.io` (the reference images are public: no login) and to Hugging Face (models, first start).
- One replica per service: Backstage holds in-memory event state, WhisperLive owns the shared model selection, and
  LiveKit is a single node without Redis. Tasks are pinned to one node (`TRADUIA_NODE`) so host data stays together.

## Deploying

`scripts/deploy.sh <env-file>` does everything below; this is what it automates, for Portainer or manual deployments:

1. **Host directories** under `TRADUIA_DATA_DIR` (default `/data/traduia`): `cache/whisperlive`, `cache/modelskit`,
   `recordings/backstage`. Ownership must allow the image users to write.
2. **Firewall** for the media ports, before the stack publishes them: [`host/livekit-media-fw`](host/livekit-media-fw/README.md).
3. **Deploy**: `docker stack deploy -c stack.yml traduia` with the variables exported
   (`set -a; . traduia.env; set +a`), or create the stack in Portainer from `stack.yml` and load the env file.
   `docker stack config -c stack.yml > /dev/null` validates it first (the output contains credentials: do not share it).
   Required variables fail with a clear message.

`FORWARDED_ALLOW_IPS` must be the source subnet of Traefik on `traefik-public` (`docker network inspect traefik-public`);
members of that network share its trust boundary.

## Configuration

The variables are listed, with comments, in [`stack.env.example`](stack.env.example). Highlights:

- **Images.** The defaults are the reference images on `ghcr.io` (release `2026.11.26`, `tag@digest`).
  Set `BSC_BACKSTAGE_IMAGE`, `BSC_STAGE_IMAGE`, `WHISPERLIVE_IMAGE`, `BSC_MODELSKIT_IMAGE` or `LIVEKIT_IMAGE` to use your own.
- **Secrets.** `init-env.py` generates `LIVEKIT_API_SECRET`, `POSTGRES_PASSWORD`, `JWT_SECRET` and the two Supabase keys
  (`SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY`: HS256 JWTs signed with `JWT_SECRET`, roles `anon` / `service_role`).
- **Optional**: `TRADUIA_DATA_DIR`, `TRAEFIK_CERT_RESOLVER`, `LIVEKIT_GWBRIDGE_CIDR` (subnet of `docker_gwbridge`),
  `TTS_PROVIDER`, `TTS_VOICE` (TTS also needs a Google credentials file that this stack does not provision).

## The built-in Supabase

Four services of `stack.yml` between the marked lines, plus the volume `supabase_db_data`: `supabase-db` (Postgres),
`supabase-auth` (GoTrue), `supabase-rest` (PostgREST) and the one-shot `supabase-init` job. No Kong, Storage, Realtime
or Studio: the application only uses auth and table/RPC access. Traefik routes
`https://supabase.<domain>/auth/v1/*` and `/rest/v1/*` (prefix stripped); no ports are published and no host files are
needed.

**Initialisation** is the `db-init` image (built from `backstage/db/` of this repository), run by the
`supabase-init` job on every deploy: it sets the service-role passwords and JWT settings, applies the SQL migrations once
each (tracked in `public.app_schema_migrations`), loads `ADMIN_EMAILS` (those addresses become approved administrators at
sign-up, registered users on the list are promoted, nobody is demoted; no administrator is hardcoded) and reloads PostgREST.
`supabase-auth` may exit once or twice at the very first start, until the job has set its password: that is normal.
The default is the reference image; to use your own build set `SUPABASE_INIT_IMAGE` (`docker build -f db/Dockerfile .` in `backstage/`).

GoTrue is configured in `stack.yml`: `GOTRUE_SITE_URL` / `GOTRUE_URI_ALLOW_LIST` (this replaces the cloud dashboard's URL
configuration), `GOTRUE_MAILER_AUTOCONFIRM=true` (**no email confirmation and no SMTP**: password-reset and other emails do
not work until SMTP is configured) and the CORS headers supabase-js sends. The database lives in the named volume
`supabase_db_data`; **there is no backup job**: back it up (for example `pg_dump -Fc` through `docker exec` on `supabase-db`).

### Using an existing Supabase (local or cloud)

1. Delete the four `supabase-*` services (the block between the marker comments) and the `supabase_db_data` volume from
   `stack.yml`. Variables `POSTGRES_PASSWORD`, `JWT_SECRET`, `ADMIN_EMAILS` and `SUPABASE_INIT_IMAGE` are then unused.
2. Create the env file with `init-env.py --existing-supabase --supabase-url … --supabase-anon-key … --supabase-service-key …`
   (or set `SUPABASE_URL`, `SUPABASE_ANON_KEY` and `SUPABASE_SERVICE_KEY` yourself).
3. Give that Supabase TraduIA's schema: the numbered files of `backstage/db/migrations`, in order.
   Supabase cloud: paste them in the SQL editor. Self-hosted: run the init image once against it
   (`docker run --rm -e PGHOST=… -e POSTGRES_PASSWORD=… -e JWT_SECRET=… -e ADMIN_EMAILS=… <init image>`; it needs the
   `supabase_admin` superuser). On cloud, after signing up, mark your profile approved and admin with an `UPDATE` on `public.profiles` in the SQL editor.
4. In that Supabase, set the **Site URL** and **Redirect URLs** to your TraduIA URLs (otherwise confirmation and
   reset links point to `localhost:3000`), and decide on email confirmation / SMTP.

## Operations

- **Administrators:** edit `ADMIN_EMAILS` and run `deploy.sh` again (the spec changes, so the init job runs again).
- **Schema changes:** add a numbered migration to `backstage/db/migrations`, rebuild the init image, update
  `SUPABASE_INIT_IMAGE` and redeploy; each file is applied once. Never edit an applied file.
- **New release:** the five image defaults in `stack.yml` are the pins of a registered release; to move to another one,
  update them from its manifest (`releases/*.json`) — `tests/test_swarm_stacks.py` checks they match — or override
  them per deployment with the `*_IMAGE` variables.
- **Updates and host-mode ports:** keep `order: stop-first`; two LiveKit tasks cannot bind the same host ports.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| All sites 502 although the services run; `Host is unreachable` from Traefik | The `traefik-public` overlay is broken, typically after crash-looping tasks (see `journalctl -u docker \| grep "fatal task error"`). Stop the crash loops first, then restart Docker; repeated task churn breaks it again. |
| Task rejected: `bind source path does not exist` | Create the host directories before deploying (`deploy.sh` does). |
| Traefik: `middleware "x@swarm" does not exist` | A router references a middleware defined by another service that has no running task. Define middlewares on the same service that uses them; do not share. |
| Browser shows "Configuration Required" | `SUPABASE_URL` / `SUPABASE_ANON_KEY` missing in the backstage environment. |
| Sign-up links point to `localhost:3000` | The Supabase Site URL / redirect allow-list is not configured (the cloud default). Built-in: `GOTRUE_SITE_URL` / `GOTRUE_URI_ALLOW_LIST` in `stack.yml`. |
| `supabase-init` fails, or `supabase-auth` / `supabase-rest` cannot log in | `docker service logs traduia_supabase-init`; `POSTGRES_PASSWORD` and `JWT_SECRET` must be the same values for all services. |
| TURN gives a relay candidate but media never connects | Check, in order: LiveKit advertises an address the relay can reach (`node_ip`, ports published); `rtc.ips.includes` limits LiveKit to the `docker_gwbridge` subnet (otherwise duplicate candidates are dropped); Chromium never pairs a relay with private-only candidates. `tests/turn-media-test` reproduces each failure. |
| `docker stack config` shows a literal `${...}` | A variable nested inside another default is not expanded by `docker stack`; keep defaults plain text. |
| Non-browser WebRTC clients: TURN `unknown certificate authority` | Their embedded root list may not trust the current Let's Encrypt chain; browsers are fine. |

## Limits and not included

- No SMTP, no database backup job, no Traefik/Portainer/NVIDIA-runtime management.
- Text-to-speech needs a Google credentials file that this stack does not provision.
- Any signed-in user can read every profile (name, email, admin flag): the schema's `profiles_select_authenticated` policy
  is `USING (true)` and sign-up is open. Review it before opening the service to untrusted users.
- Data from another Supabase project is not migrated; a new deployment starts empty (schema and administrators only).

References: [Traefik Swarm provider](https://doc.traefik.io/traefik/reference/install-configuration/providers/swarm/),
[LiveKit ports and firewall](https://docs.livekit.io/transport/self-hosting/ports-firewall/),
[NVIDIA runtime configuration](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
