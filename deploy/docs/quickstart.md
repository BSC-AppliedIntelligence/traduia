# Quick start: TraduIA on Docker Swarm

Get the whole system running on one GPU server with the **reference images**, a **self-hosted Supabase** and external
audio through **TURN over TLS**. A handful of commands once the prerequisites are in place; about 15 minutes plus the first
download of images and models.

What you get, behind your own domain: the operator application (`backstage.<domain>`), the listener page
(`stage.<domain>`), speech recognition (WhisperLive) and translation (ModelsKit) on the GPU, LiveKit for the audio, and a
Supabase (login + database) that is part of the same stack. Want to adapt the system or build your own images? Read the
[developer guide](developer-guide.md) afterwards. Details of every part are in the [Swarm reference](../swarm/README.md).

## Before you start

| You need | Notes |
| --- | --- |
| A Linux AMD64 server with an NVIDIA GPU | driver 580.173.02+, NVIDIA Container Toolkit, Docker 24+ with **`nvidia` as the default runtime** |
| Docker in Swarm mode | `docker swarm init` on the server (a single node is fine) |
| Traefik in that swarm | v3, entrypoints `web`/`websecure`, a Let's Encrypt resolver named `le` (HTTP challenge), overlay network `traefik-public`. [Requirements](../swarm/README.md#prerequisites); this repository does not deploy Traefik |
| A domain | A records for `<domain>`, `backstage.`, `stage.`, `livekit.`, `turn.` and `supabase.<domain>` (or a wildcard) pointing to the server |
| Internet access | `ghcr.io` (the images are public, no login) and Hugging Face (models, first start) |
| Ports | **80 and 443** open to the Internet. Do **not** open 7881/7882: the deployment keeps them closed on purpose |

## 1. Get the files

```bash
git clone https://github.com/BSC-AppliedIntelligence/traduia.git
cd traduia/deploy
```

The reference images (Backstage, Stage, WhisperLive, ModelsKit and the database initialisation image) are public on
`ghcr.io/bsc-appliedintelligence/traduia/` and pinned by digest in `swarm/stack.yml`: there is nothing to build.

## 2. Create your configuration

```bash
python3 swarm/scripts/init-env.py \
  --domain example.org --public-ip 203.0.113.10 \
  --admin-email you@example.org --output traduia.env
```

This writes `traduia.env` (private, never overwritten) with freshly generated secrets, detects the swarm node and the
Traefik subnet, and lists the administrator address(es). **Keep the file safe and out of git**: it holds the database
password and the keys. Add `--admin-email` more than once for more administrators.

## 3. Deploy

```bash
swarm/scripts/deploy.sh traduia.env
```

It creates the data directories, installs the firewall rule that keeps the media ports off the Internet (uses `sudo`),
checks the configuration, deploys the stack and waits until everything answers. The first start downloads the images
and the speech models, so allow several minutes. Run the same command again to apply any later change.

## 4. First login

1. Open `https://backstage.example.org/login` and **sign up with the administrator address**: it is approved and made
   administrator automatically (there is no confirmation email).
2. Create an event, add the channels, and under *Endpoints* use the internal services: ASR `whisperlive:9090`, translator
   `http://modelskit:8000`, source language Catalan.
3. Open the speaker page, speak, and check the captions and translations in another browser on **another network**.

Optional media check from the server (a headless browser sends a test tone through TURN):

```bash
TRADUIA_DOMAIN=example.org LIVEKIT_API_KEY=… LIVEKIT_API_SECRET=… swarm/tests/turn-media-test/run.sh relay
```

Both values are in `traduia.env`.

## Already have a Supabase?

Use it instead of the built-in one (self-hosted elsewhere, or Supabase cloud):

1. Create the env file with `--existing-supabase --supabase-url https://… --supabase-anon-key … --supabase-service-key …`
   instead of `--admin-email`.
2. Delete the `supabase-*` services and the `supabase_db_data` volume from `swarm/stack.yml` (the block between the marker
   comments, which says so).
3. Give that Supabase the schema and set its Site URL / redirect URLs: [steps](../swarm/README.md#using-an-existing-supabase-local-or-cloud).

Then continue with step 3. The init image is not needed.

## Using Portainer instead of `deploy.sh`

Create the host directories and install the firewall as described in the [reference](../swarm/README.md#deploying),
then create the stack from `swarm/stack.yml` and load `traduia.env` as its
environment.

## Day 2

| I want to… | Do |
| --- | --- |
| Add or change administrators | edit `ADMIN_EMAILS` in `traduia.env`, run `deploy.sh traduia.env` again |
| Update | `git pull` (new image pins come with new releases), run `deploy.sh traduia.env` again |
| See what is wrong | `docker stack ps traduia --no-trunc`, `docker service logs traduia_<service>`; symptoms are tabulated in the [troubleshooting](../swarm/README.md#troubleshooting) |
| Back up | the database volume `traduia_supabase_db_data` (for example `pg_dump -Fc` via `docker exec` on `supabase-db`), plus `/data/traduia/recordings` |
| Use my own images | set `BSC_BACKSTAGE_IMAGE` (and the other `*_IMAGE` variables) in `traduia.env`: [developer guide](developer-guide.md#reference-images-and-your-own) |

Good to know: sign-ups are auto-confirmed and there is no SMTP, so *forgot password* e-mails are not sent; text-to-speech
needs a Google credentials file that is not part of this stack; any signed-in user can see the list of profiles (see the
[limits](../swarm/README.md#limits-and-not-included)).
