"""Static checks of the database initialisation image (db/). The full behaviour is covered by scripts/test_db_init.sh."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "db"
MIGRATIONS = sorted((DB / "migrations").glob("*.sql"))


def test_migrations_are_numbered_ordered_and_unique():
    names = [p.name for p in MIGRATIONS]
    numbers = [n.split("_", 1)[0] for n in names]
    assert all(re.fullmatch(r"\d{3}", n) for n in numbers)
    assert numbers == sorted(numbers) and len(numbers) == len(set(numbers))
    assert numbers[:5] == ["001", "002", "003", "004", "005"]


def test_no_administrator_is_hardcoded_in_the_init_image_or_the_manual_scripts():
    for path in [*DB.glob("**/*"), *(ROOT / "sql").glob("*.sql")]:
        if path.is_file() and path.suffix in {".sql", ".sh"}:
            emails = set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", path.read_text()))
            assert emails <= {"your@email.com", "you@example.org"}, f"hardcoded address in {path.relative_to(ROOT)}"


def test_admin_list_is_private_and_drives_the_signup_trigger():
    sql = (DB / "migrations" / "010_admin_bootstrap.sql").read_text()
    for needle in ("ENABLE ROW LEVEL SECURITY", "REVOKE ALL ON public.bootstrap_admin_emails",
                   "SECURITY DEFINER", "public.is_bootstrap_admin(NEW.email)"):
        assert needle in sql


def test_init_script_parses_and_keeps_secrets_off_the_command_line():
    script = DB / "init" / "db-init.sh"
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    text = script.read_text()
    assert "set -euo pipefail" in text
    # Credentials travel through the environment (PGPASSWORD / psql backticks), never as psql arguments.
    assert 'PGPASSWORD="$POSTGRES_PASSWORD" psql' in text
    for line in text.splitlines():
        if re.match(r"\s*as_(admin|owner)\s", line):  # invocations, not the helper definitions
            assert "$POSTGRES_PASSWORD" not in line and "$JWT_SECRET" not in line, line
    # Superuser access is limited to the role passwords / JWT settings; schema objects and admins belong to postgres.
    assert len(re.findall(r'^as_admin -f "\$INIT_DIR/roles-and-jwt.sql"', text, re.M)) == 1
    assert 'as_owner -v admin_emails=' in text
    assert 'as_owner -1 -f "$f"' in text


def test_roles_and_jwt_sql_reads_the_environment():
    sql = (DB / "init" / "roles-and-jwt.sql").read_text()
    for needle in ('echo "$POSTGRES_PASSWORD"', 'echo "$JWT_SECRET"', "ALTER ROLE authenticator", "ALTER ROLE supabase_auth_admin"):
        assert needle in sql


def test_dockerfile_copies_existing_paths_and_the_build_context_is_scoped():
    dockerfile = (DB / "Dockerfile").read_text()
    for source in re.findall(r"^COPY (\S+) ", dockerfile, re.M):
        assert (ROOT / source).exists(), source
    assert "ENTRYPOINT" in dockerfile and "USER" in dockerfile
    # The app image excludes db/; the db image build re-includes only what it needs.
    assert "db/" in [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()]
    scoped = [line.strip() for line in (DB / "Dockerfile.dockerignore").read_text().splitlines() if line.strip()]
    assert scoped == ["*", "!db/migrations", "!db/init"]


def test_test_script_parses():
    for script in (ROOT / "scripts" / "test_db_init.sh",):
        assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
