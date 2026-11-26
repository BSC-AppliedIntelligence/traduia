-- Run by db-init.sh as supabase_admin (the only superuser of the supabase/postgres image).
-- Idempotent. Values come from the environment, never from the command line.
\set pgpass `echo "$POSTGRES_PASSWORD"`
\set jwt_secret `echo "$JWT_SECRET"`
\set jwt_exp `echo "${JWT_EXP:-3600}"`
\set dbname `echo "${PGDATABASE:-postgres}"`

-- GoTrue and PostgREST log in with these roles; the image creates them without a usable password.
ALTER ROLE authenticator WITH PASSWORD :'pgpass';
ALTER ROLE supabase_auth_admin WITH PASSWORD :'pgpass';

-- Settings read by Supabase helper functions.
ALTER DATABASE :"dbname" SET "app.settings.jwt_secret" TO :'jwt_secret';
ALTER DATABASE :"dbname" SET "app.settings.jwt_exp" TO :'jwt_exp';
