# Database initialisation image

`db/` builds a small one-shot image, **`db-init`**, that prepares a **self-hosted Supabase**
(Postgres + GoTrue + PostgREST) for Backstage. Run it on every deployment: it is idempotent. With a managed
Supabase cloud project you have no superuser access; run the files of `db/migrations/` by hand in the SQL editor
(in numeric order) instead.

## What it does

1. Waits for the database (and its `auth` schema) to accept connections.
2. As `supabase_admin` (the only superuser of the `supabase/postgres` image): sets the passwords of the `authenticator`
   and `supabase_auth_admin` roles that PostgREST and GoTrue log in with, and the `app.settings.jwt_*` database settings.
   This replaces host-mounted init scripts: the Postgres container needs no files.
3. As `postgres`: applies every `db/migrations/*.sql` **once**, in name order, tracking them in
   `public.app_schema_migrations` (the schema is owned by `postgres`, as with the Supabase dashboard's SQL editor).
4. Loads `ADMIN_EMAILS` into `public.bootstrap_admin_emails` and promotes registered users on the list.
5. Sends `NOTIFY pgrst, 'reload schema'` so PostgREST sees the new tables.

GoTrue and PostgREST may start before the job has set their passwords; they simply retry until it has run.

## Configuration

| Variable | Required | Meaning |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | yes | Password of `postgres` and `supabase_admin`; also assigned to the service roles |
| `JWT_SECRET` | yes | The secret GoTrue and PostgREST use to sign/verify JWTs |
| `ADMIN_EMAILS` | no | Comma-separated addresses (case-insensitive) that become **approved administrators** when they sign up |
| `JWT_EXP` | no | Token lifetime in seconds, default `3600` |
| `PGHOST`, `PGPORT`, `PGDATABASE` | no | Defaults `db`, `5432`, `postgres` |
| `WAIT_SECONDS` | no | How long to wait for the database, default `600` |

Credentials are read from the environment and never appear on a command line. The image contains no secrets.

```bash
docker run --rm --network <supabase-network> \
  -e POSTGRES_PASSWORD -e JWT_SECRET -e ADMIN_EMAILS="admin@example.org" \
  ghcr.io/bsc-appliedintelligence/traduia/db-init:2026.11.26
```

In Docker Swarm run it as a job service (`deploy.mode: replicated-job`); changing `ADMIN_EMAILS` in the stack
environment and redeploying runs it again.

## Administrators without a hardcoded user

`handle_new_user` creates a profile as approved + admin when the sign-up address is on the list loaded from
`ADMIN_EMAILS` (the list lives in a table with row-level security and no grants for API roles; only the
`SECURITY DEFINER` trigger reads it). Everyone else still waits for approval. Removing an address never demotes
anybody. Without the setting, approve users by hand with `sql/B_autorizar_admin.sql`.

## Migrations

- Add new changes as new numbered files (`011_...sql`) and never edit an applied one; the job applies each file once.
- Everything in `db/migrations/` is applied automatically, so keep one-off or manual scripts in `sql/`.
- On a database that already had `001`–`005` applied by hand, insert their filenames into `public.app_schema_migrations`
  before the first run.

## Image, build and publish

Published as `ghcr.io/bsc-appliedintelligence/traduia/db-init` (tags are release dates, for example `2026.11.26`; pin the digest).
To build your own, from the `backstage` directory (the build context is limited to `db/migrations` and `db/init` by
`db/Dockerfile.dockerignore`):

```bash
docker build -f db/Dockerfile -t traduia-db-init .
```

Publish with the helper that every component ships (`scripts/docker_build_and_push.sh`), choosing the name and tag:

```bash
IMAGE_NAME=ghcr.io/<your-org>/traduia/db-init:$(git rev-parse --short HEAD) \
DOCKERFILE_PATH=db/Dockerfile PLATFORMS=linux/amd64 PUSH=true bash scripts/docker_build_and_push.sh
```

## Tests

- `pytest tests/test_db_init.py tests/test_frontend_config.py` — static checks and the frontend settings (the behaviour
  tests need `node`).
- `bash scripts/test_db_init.sh` — builds the image and runs it against real `supabase/postgres`, GoTrue and PostgREST
  containers (needs Docker and Internet): role passwords, migrations applied once, sign-up trigger, administrators,
  row-level security and idempotency. Verified with `supabase/postgres:15.8.1.085`, `supabase/gotrue:v2.186.0` and
  `postgrest/postgrest:v13.0.8`.
