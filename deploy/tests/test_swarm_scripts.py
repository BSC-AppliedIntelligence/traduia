"""Tests of the streamlined-deployment helpers in swarm/scripts: init-env.py and deploy.sh.

deploy.sh runs against stub `docker`, `sudo`, `systemctl` and `curl` commands that log what they
receive, so ordering, idempotency and error handling are checked without Docker or root.
"""
import base64
import hashlib
import hmac
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "swarm" / "scripts"
INIT_ENV = SCRIPTS / "init-env.py"

RENDERED = """services:
  backstage:
    image: registry.example/backstage:t@sha256:abc
  supabase-db:
    image: supabase/postgres:15
"""


def read_env(path):
    return dict(line.split("=", 1) for line in Path(path).read_text().splitlines() if line)


def run_init_env(*args):
    return subprocess.run([sys.executable, str(INIT_ENV), *args], capture_output=True, text=True)


COMMON = ["--domain", "example.org", "--public-ip", "203.0.113.10", "--node", "node1", "--forwarded-allow-ips", "10.0.6.0/24"]


class InitEnvTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory)
        self.out = self.directory / "traduia.env"

    def test_builtin_supabase_environment_is_complete_private_and_consistent(self):
        result = run_init_env(*COMMON, "--admin-email", "a@example.org", "--admin-email", "B@Example.org", "--output", str(self.out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)
        env = read_env(self.out)
        self.assertEqual(set(env), {"TRADUIA_DOMAIN", "TRADUIA_PUBLIC_IP", "TRADUIA_NODE", "FORWARDED_ALLOW_IPS", "LIVEKIT_API_KEY",
                                    "LIVEKIT_API_SECRET", "SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY",
                                    "POSTGRES_PASSWORD", "JWT_SECRET", "ADMIN_EMAILS"})
        self.assertEqual(env["SUPABASE_URL"], "https://supabase.example.org")
        self.assertEqual(env["ADMIN_EMAILS"], "a@example.org,B@Example.org")
        self.assertGreaterEqual(len(env["LIVEKIT_API_SECRET"]), 32)
        # the keys are valid HS256 JWTs signed with the generated secret, with the right roles
        for key, role in (("SUPABASE_ANON_KEY", "anon"), ("SUPABASE_SERVICE_KEY", "service_role")):
            header, payload, signature = env[key].split(".")
            expected = base64.urlsafe_b64encode(hmac.new(env["JWT_SECRET"].encode(), f"{header}.{payload}".encode(),
                                                         hashlib.sha256).digest()).rstrip(b"=").decode()
            self.assertEqual(signature, expected)
            self.assertIn(f'"role":"{role}"', base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode())
        for secret in (env["JWT_SECRET"], env["POSTGRES_PASSWORD"], env["LIVEKIT_API_SECRET"], env["SUPABASE_SERVICE_KEY"]):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_existing_supabase_mode_leaves_out_the_builtin_variables(self):
        result = run_init_env(*COMMON, "--existing-supabase", "--supabase-url", "https://abc.supabase.co/",
                              "--supabase-anon-key", "eyJa", "--supabase-service-key", "eyJs", "--output", str(self.out))
        self.assertEqual(result.returncode, 0, result.stderr)
        env = read_env(self.out)
        self.assertEqual((env["SUPABASE_URL"], env["SUPABASE_ANON_KEY"], env["SUPABASE_SERVICE_KEY"]), ("https://abc.supabase.co", "eyJa", "eyJs"))
        for name in ("POSTGRES_PASSWORD", "JWT_SECRET", "ADMIN_EMAILS"):
            self.assertNotIn(name, env)

    def test_invalid_input_is_rejected_without_writing_a_file(self):
        cases = [
            (["--domain", "localhost", "--public-ip", "8.8.8.8"], "valid domain"),
            (["--domain", "example.org", "--public-ip", "10.1.2.3"], "public IPv4"),
            (["--domain", "example.org", "--public-ip", "not-an-ip"], "IP address"),
            (["--domain", "example.org", "--public-ip", "8.8.8.8", "--admin-email", "nope"], "email"),
            (["--domain", "example.org", "--public-ip", "8.8.8.8", "--existing-supabase"], "--supabase-url"),
        ]
        for args, message in cases:
            result = run_init_env("--node", "n", "--forwarded-allow-ips", "10.0.0.0/24", *args, "--output", str(self.out))
            self.assertNotEqual(result.returncode, 0, args)
            self.assertIn(message, result.stderr)
            self.assertFalse(self.out.exists(), args)

    def test_never_overwrites(self):
        self.out.write_text("keep\n")
        result = run_init_env(*COMMON, "--output", str(self.out))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.out.read_text(), "keep\n")


class StubbedScriptTests(unittest.TestCase):
    """Run deploy.sh with logging stubs in front of PATH."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory)
        d = self.directory
        (d / "bin").mkdir()
        self.log = d / "log"
        self.log.write_text("")
        (d / "rendered.yml").write_text(RENDERED)
        stubs = {
            "docker": """#!/bin/sh
echo "docker $*" >> "$STUB_LOG"
case "$1 $2" in
  "info --format")
    case "$3" in *LocalNodeState*) echo "${STUB_SWARM:-active}";; *ControlAvailable*) echo true;; esac ;;
  "stack config") cat "$STUB_RENDERED" ;;
  "stack services") printf 'traduia_backstage 1/1\\ntraduia_supabase-init 0/1 (1/1 completed)\\n' ;;
esac
exit "${STUB_DOCKER_RC:-0}"
""",
            "sudo": '#!/bin/sh\necho "sudo $*" >> "$STUB_LOG"\n',
            "systemctl": '#!/bin/sh\necho "systemctl $*" >> "$STUB_LOG"\n[ "$1" = "is-active" ] && exit "${STUB_FW_RC:-1}"\nexit 0\n',
            "curl": '#!/bin/sh\necho "curl $*" >> "$STUB_LOG"\n',
            "git": '#!/bin/sh\necho "git $*" >> "$STUB_LOG"\n',
        }
        for name, body in stubs.items():
            path = d / "bin" / name
            path.write_text(body)
            path.chmod(0o755)
        # a private copy of the swarm folder layout so deploy.sh finds its siblings
        swarm = d / "swarm"
        (swarm / "scripts").mkdir(parents=True)
        shutil.copy(SCRIPTS / "deploy.sh", swarm / "scripts" / "deploy.sh")
        (swarm / "stack.yml").write_text("services:\n  supabase-db:\n    image: x\n")
        fw = swarm / "host" / "livekit-media-fw"
        fw.mkdir(parents=True)
        (fw / "livekit-media-fw.sh").write_text("#!/bin/sh\n")
        (fw / "livekit-media-fw.service").write_text("[Unit]\n")
        self.env_file = d / "traduia.env"
        self.write_env()

    def write_env(self, **overrides):
        values = {"TRADUIA_DOMAIN": "example.org", "TRADUIA_PUBLIC_IP": "203.0.113.10", "TRADUIA_NODE": "n1",
                  "FORWARDED_ALLOW_IPS": "10.0.6.0/24", "LIVEKIT_API_KEY": "k", "LIVEKIT_API_SECRET": "S3CRET-LIVEKIT",
                  "SUPABASE_URL": "https://supabase.example.org", "SUPABASE_ANON_KEY": "anon", "SUPABASE_SERVICE_KEY": "S3CRET-SERVICE",
                  "POSTGRES_PASSWORD": "S3CRET-PG", "JWT_SECRET": "S3CRET-JWT",
                  "TRADUIA_DATA_DIR": str(self.directory / "data")}
        values.update(overrides)
        self.env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items() if v is not None))

    def run_script(self, script, *args, **extra):
        env = {"PATH": f"{self.directory / 'bin'}:/usr/bin:/bin", "STUB_LOG": str(self.log), "STUB_RENDERED": str(self.directory / "rendered.yml"),
               "HOME": str(self.directory), **extra}
        return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=env)

    def deploy(self, *args, **extra):
        return self.run_script(self.directory / "swarm" / "scripts" / "deploy.sh", str(self.env_file), "--no-wait", *args, **extra)

    def calls(self):
        return [line for line in self.log.read_text().splitlines()]

    def test_missing_variable_fails_before_touching_docker(self):
        self.write_env(LIVEKIT_API_SECRET=None, POSTGRES_PASSWORD=None)
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LIVEKIT_API_SECRET", result.stderr)
        self.assertIn("POSTGRES_PASSWORD", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_without_the_builtin_supabase_block_its_variables_are_not_required(self):
        (self.directory / "swarm" / "stack.yml").write_text("services:\n  backstage:\n    image: x\n")
        self.write_env(POSTGRES_PASSWORD=None, JWT_SECRET=None)
        self.assertEqual(self.deploy().returncode, 0)

    def test_requires_a_swarm_manager(self):
        result = self.deploy(STUB_SWARM="inactive")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("swarm", result.stderr)
        self.assertFalse(any("stack deploy" in call for call in self.calls()))

    def test_invalid_configuration_stops_before_deploying(self):
        result = self.deploy(STUB_DOCKER_RC="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call.startswith("docker stack deploy") for call in self.calls()))

    def test_first_run_prepares_host_then_deploys_in_order(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        order = [next(i for i, call in enumerate(calls) if needle in call) for needle in
                 ("sudo install -m 755", "docker stack config", "docker stack deploy -c")]
        self.assertEqual(order, sorted(order), calls)
        self.assertTrue(any("systemctl enable --now livekit-media-fw.service" in call for call in calls))
        data = self.directory / "data"
        for sub in ("cache/whisperlive", "cache/modelskit", "recordings/backstage"):
            self.assertTrue((data / sub).is_dir(), sub)
        self.assertIn("-c " + str(self.directory / "swarm" / "stack.yml") + " traduia", " ".join(calls))

    def test_second_run_is_idempotent(self):
        self.assertEqual(self.deploy().returncode, 0)
        self.log.write_text("")
        result = self.deploy(STUB_FW_RC="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertFalse(any("sudo" in call for call in calls), calls)
        self.assertTrue(any(call.startswith("docker stack deploy") for call in calls))

    def test_stack_name_and_skipping_host_setup(self):
        result = self.deploy("--stack", "mystack", "--no-host-setup")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertTrue(any(call.endswith(" mystack") for call in calls if call.startswith("docker stack deploy")))
        self.assertFalse(any("sudo" in call or "systemctl" in call for call in calls))

    def test_waiting_succeeds_when_every_service_and_the_job_are_done(self):
        result = self.run_script(self.directory / "swarm" / "scripts" / "deploy.sh", str(self.env_file), WAIT_SECONDS="30")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any("supabase.example.org/auth/v1/health" in call for call in self.calls()))
        self.assertIn("https://backstage.example.org/login", result.stdout)

    def test_secrets_are_never_echoed(self):
        result = self.deploy()
        for secret in ("S3CRET-LIVEKIT", "S3CRET-SERVICE", "S3CRET-PG", "S3CRET-JWT"):
            self.assertNotIn(secret, result.stdout + result.stderr)
            self.assertFalse(any(secret in call for call in self.calls()), secret)


if __name__ == "__main__":
    unittest.main()
