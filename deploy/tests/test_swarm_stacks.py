"""Static checks for the Swarm reference deployment (swarm/): no Docker, network or YAML library needed.

They lock the lessons of the reference deployment: image pins equal the registered release, secrets only come from
the stack environment, every Traefik router uses a middleware defined by its own service, only LiveKit publishes
(firewalled) media ports, and the helper scripts behave.
"""
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWARM = ROOT / "swarm"
APP_STACK = SWARM / "stack.yml"
RELEASE = ROOT / "releases" / "2026.11.26.json"
REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")


def service_blocks(text):
    """Split the top-level `services:` mapping into {name: block text} using indentation only."""
    lines = text.splitlines()
    blocks, current = {}, None
    for line in lines[lines.index("services:") + 1:]:
        if line and not line.startswith(" "):
            break
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            current = match.group(1)
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return {name: "\n".join(body) for name, body in blocks.items()}


def code_lines(text):
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def load_module(path):
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StackDefinitionTests(unittest.TestCase):
    APP = ("backstage", "stage", "whisperlive", "modelskit", "livekit")
    SUPABASE = ("supabase-db", "supabase-auth", "supabase-rest", "supabase-init")

    def setUp(self):
        self.stack = APP_STACK.read_text()
        services = service_blocks(self.stack)
        self.app_services = {name: block for name, block in services.items() if name in self.APP}
        self.supabase_services = {name: block for name, block in services.items() if name in self.SUPABASE}
        self.supabase = "\n".join(self.supabase_services.values())
        self.app = "\n".join(self.app_services.values())

    def test_expected_services_exist(self):
        self.assertEqual(set(service_blocks(self.stack)), set(self.APP) | set(self.SUPABASE))
        self.assertEqual(set(self.app_services), set(self.APP))
        self.assertEqual(set(self.supabase_services), set(self.SUPABASE))

    def test_supabase_block_is_delimited_and_removable(self):
        start = self.stack.index("\n  # Self-hosted Supabase:")
        end = self.stack.index("\n  # ===================== END of the self-hosted Supabase block")
        block = self.stack[start:end]
        self.assertIn("USING AN EXISTING SUPABASE", block)
        for name in self.SUPABASE:  # exactly the supabase services live inside the markers
            self.assertIn(f"\n  {name}:\n", block)
        for name in self.APP:
            self.assertNotIn(f"\n  {name}:\n", block)
        # nothing outside the block (other than the volume) may depend on the supabase services
        outside = self.stack[:start] + self.stack[end:]
        self.assertNotRegex(code_lines(outside), r"supabase-(db|auth|rest|init)\b")
        self.assertEqual(re.findall(r"(?m)^volumes:\n  (\w+):", self.stack), ["supabase_db_data"])
        self.assertEqual(set(re.findall(r"supabase_db_data", code_lines(self.stack))), {"supabase_db_data"})
        # without the block the application keeps working with an external SUPABASE_URL / keys
        for service in ("backstage", "stage"):
            self.assertIn("SUPABASE_URL: ${SUPABASE_URL:?", self.app_services[service])

    def test_application_images_are_the_registered_release_pins(self):
        services = json.loads(RELEASE.read_text())["services"]
        variables = {"backstage": "BSC_BACKSTAGE_IMAGE", "stage": "BSC_STAGE_IMAGE", "whisperlive": "WHISPERLIVE_IMAGE",
                     "modelskit": "BSC_MODELSKIT_IMAGE", "livekit": "LIVEKIT_IMAGE"}
        for name, block in self.app_services.items():
            # The registered pin is the default; the variable lets an operator substitute their own build.
            pin = f"image: ${{{variables[name]}:-{services[name]['image']}@{services[name]['digest']}}}"
            self.assertIn(pin, block, f"{name} does not default to the registered release pin")

    def test_every_image_is_pinned_by_digest(self):
        for text in (self.app, self.supabase):
            for line in code_lines(text).splitlines():
                if line.strip().startswith("image:"):
                    ref = line.split("image:", 1)[1].strip()
                    # Supabase component images are pinned by tag, the application images by tag@digest, and the
                    # init image has a pinned default that the operator may replace.
                    self.assertRegex(ref, r"[:@$]", f"unpinned image {ref}")
        for block in self.app_services.values():
            self.assertRegex(block, r"image: \$\{[A-Z_]+:-\S+@sha256:[0-9a-f]{64}\}")

    def test_no_env_files_and_no_docker_secrets(self):
        body = code_lines(self.stack)
        self.assertNotIn("env_file", body)
        self.assertNotRegex(body, r"(?m)^secrets:")

    def test_no_literal_credentials_or_deployment_values(self):
        for path in (APP_STACK, *SWARM.glob("**/*.example")):
            body = code_lines(path.read_text())
            self.assertNotRegex(body, r"eyJ[A-Za-z0-9_-]{15,}", f"JWT literal in {path.name}")
            self.assertNotRegex(body, r"(ptr_|sbp_)[A-Za-z0-9]", f"token literal in {path.name}")
        body = code_lines(self.stack)
        self.assertNotIn("langtech.es", body, "hostnames must come from TRADUIA_DOMAIN")
        self.assertNotRegex(body, r"node\.hostname == (?!\$\{)", "node must come from TRADUIA_NODE")

    def test_every_router_middleware_is_defined_by_the_same_service(self):
        for name, block in service_blocks(self.stack).items():
            defined = set(re.findall(r"traefik\.http\.middlewares\.([\w-]+)\.", block))
            for used in re.findall(r"traefik\.http\.routers\.[\w-]+\.middlewares: ([\w@,-]+)", block):
                for middleware in used.split(","):
                    self.assertNotIn("@", middleware, "cross-provider middleware reference")
                    self.assertIn(middleware, defined, f"{name} uses middleware {middleware} that it does not define itself")

    def test_middleware_names_are_unique_across_services(self):
        seen = {}
        for name, block in service_blocks(self.stack).items():
            for middleware in set(re.findall(r"traefik\.http\.middlewares\.([\w-]+)\.", block)):
                self.assertNotIn(middleware, seen, f"{middleware} defined by {name} and {seen.get(middleware)}")
                seen[middleware] = name

    def test_only_livekit_publishes_ports_and_only_the_media_ports(self):
        for name, block in self.app_services.items():
            published = re.findall(r"(?m)^    ports:", block)
            self.assertEqual(bool(published), name == "livekit", f"unexpected ports in {name}")
        livekit = self.app_services["livekit"]
        self.assertEqual(sorted(re.findall(r"published: (\d+)", livekit)), ["7881", "7882"])
        self.assertEqual(livekit.count("mode: host"), 2)
        for name, block in self.supabase_services.items():
            self.assertNotRegex(block, r"(?m)^    ports:", f"{name} must not publish ports")
        self.assertNotRegex(self.stack, r"published: (?!7881|7882)\d+")

    def test_livekit_is_configured_from_the_environment_for_turn_only_access(self):
        livekit = self.app_services["livekit"]
        self.assertIn("LIVEKIT_CONFIG: |", livekit)
        for needle in ("node_ip: ${TRADUIA_PUBLIC_IP", "external_tls: true", "tls_port: 5349",
                       "${LIVEKIT_GWBRIDGE_CIDR:-172.18.0.0/16}", "HostSNI(`turn.${TRADUIA_DOMAIN}`)",
                       "traefik.tcp.services.traduia-livekit-turn.loadbalancer.server.port: '5349'"):
            self.assertIn(needle, livekit)

    def test_backstage_reads_its_frontend_config_from_the_environment(self):
        # The image injects the Supabase settings at start-up: no config.js override is mounted.
        self.assertNotIn("config.js", self.app_services["backstage"])
        for service in ("backstage", "stage"):
            for name in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
                self.assertIn(f"{name}: ${{{name}:?", self.app_services[service], f"{service} lacks {name}")

    def test_no_nested_variable_expansion(self):
        # `docker stack` leaves ${A:-x${B}} unresolved (it prints a literal "${B}"): defaults must be plain text.
        self.assertNotRegex(code_lines(self.stack), r"\$\{[^}]*:[-?][^}]*\$\{")

    def test_env_examples_list_every_required_variable(self):
        example = SWARM / "stack.env.example"
        listed = set(re.findall(r"(?m)^#?\s*([A-Z][A-Z0-9_]+)=", example.read_text()))
        referenced = set(REF.findall(code_lines(self.stack)))
        self.assertEqual(referenced - listed, set(), f"{example.name} misses variables used by the stack")
        self.assertEqual(listed - referenced, set(), f"{example.name} lists variables the stack does not use")

    def test_supabase_stack_is_configured_without_email_and_with_its_own_urls(self):
        gotrue = self.supabase_services["supabase-auth"]
        for needle in ("GOTRUE_SITE_URL: https://${TRADUIA_DOMAIN}", "GOTRUE_URI_ALLOW_LIST:", "GOTRUE_CORS_ALLOWED_HEADERS:",
                       "GOTRUE_MAILER_AUTOCONFIRM: 'true'"):
            self.assertIn(needle, gotrue)
        bootstrap = self.supabase_services["supabase-init"]
        self.assertIn("PGHOST: supabase-db", bootstrap)
        for service in ("supabase-auth", "supabase-rest"):
            self.assertIn("@supabase-db:5432", self.supabase_services[service])
        self.assertIn("mode: replicated-job", bootstrap)
        self.assertIn("image: ${SUPABASE_INIT_IMAGE:-ghcr.io/bsc-appliedintelligence/traduia/db-init:", bootstrap)
        for variable in ("POSTGRES_PASSWORD", "JWT_SECRET", "ADMIN_EMAILS"):
            self.assertIn(f"{variable}: ${{{variable}", bootstrap)

    def test_supabase_needs_no_host_files(self):
        # Initialisation is an image (bsc-backstage-db-init): no bind mounts, no init scripts, no SQL in this repository.
        self.assertNotIn("type: bind", self.supabase)
        for name, block in self.supabase_services.items():
            for line in block.splitlines():
                if re.match(r"\s+- [^\s]+:/", line):
                    self.assertTrue(line.strip().startswith("- supabase_db_data:"), f"host mount in {name}: {line.strip()}")
        self.assertNotIn("JWT_SECRET", self.supabase_services["supabase-db"], "the database container must not need the JWT secret")
        self.assertFalse((SWARM / "supabase").exists(), "the Supabase block is part of stack.yml; no separate folder or files")


class HostFirewallTests(unittest.TestCase):
    def test_media_ports_are_dropped_on_the_external_interface_for_both_ip_versions(self):
        script = (SWARM / "host" / "livekit-media-fw" / "livekit-media-fw.sh").read_text()
        self.assertEqual(len(re.findall(r"^rule4 DOCKER-USER .*-j DROP$", script, re.M)), 2)
        self.assertEqual(len(re.findall(r"^rule4 INPUT .*-j DROP$", script, re.M)), 2)
        self.assertEqual(len(re.findall(r"^rule6 INPUT .*-j DROP$", script, re.M)), 2)
        for rule in re.findall(r"^rule[46] .*-j DROP$", script, re.M):
            self.assertIn('-i "$EXT"', rule)
        self.assertIn("ip -o route get", script)
        unit = (SWARM / "host" / "livekit-media-fw" / "livekit-media-fw.service").read_text()
        for needle in ("After=docker.service", "PartOf=docker.service", "ExecStart=/usr/local/sbin/livekit-media-fw.sh"):
            self.assertIn(needle, unit)

    def test_scripts_parse(self):
        for path in (SWARM / "host" / "livekit-media-fw" / "livekit-media-fw.sh",
                     SWARM / "scripts" / "deploy.sh",
                     SWARM / "tests" / "turn-media-test" / "run.sh"):
            result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{path.name}: {result.stderr}")


class SecretsGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module(SWARM / "scripts" / "generate-supabase-secrets.py")

    @staticmethod
    def decode(token, secret):
        import base64, hashlib, hmac
        header, payload, signature = token.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)
        expected = base64.urlsafe_b64encode(
            hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        return json.loads(base64.urlsafe_b64decode(pad(payload))), hmac.compare_digest(signature, expected)

    def test_keys_are_valid_hs256_jwts_with_the_expected_roles(self):
        values = self.module.generate(now=1_800_000_000)
        for key, role in (("ANON_KEY", "anon"), ("SERVICE_ROLE_KEY", "service_role")):
            claims, valid = self.decode(values[key], values["JWT_SECRET"])
            self.assertTrue(valid)
            self.assertEqual((claims["role"], claims["iss"], claims["iat"]), (role, "supabase", 1_800_000_000))
            self.assertGreater(claims["exp"], claims["iat"] + 9 * 365 * 86400)
        self.assertEqual(values["SUPABASE_ANON_KEY"], values["ANON_KEY"])
        self.assertEqual(values["SUPABASE_SERVICE_KEY"], values["SERVICE_ROLE_KEY"])
        self.assertGreaterEqual(len(values["JWT_SECRET"]), 32)
        self.assertNotEqual(values["POSTGRES_PASSWORD"], values["JWT_SECRET"])

    def test_cli_writes_a_private_file_prints_no_secret_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "secrets.env"
            result = subprocess.run([sys.executable, str(SWARM / "scripts" / "generate-supabase-secrets.py"),
                                     "--output", str(target)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            values = dict(line.split("=", 1) for line in target.read_text().splitlines())
            self.assertIn("ANON_KEY", values)
            for secret in values.values():
                self.assertNotIn(secret, result.stdout + result.stderr)
            again = subprocess.run([sys.executable, str(SWARM / "scripts" / "generate-supabase-secrets.py"),
                                    "--output", str(target)], capture_output=True, text=True)
            self.assertEqual(again.returncode, 1)
            self.assertEqual(dict(line.split("=", 1) for line in target.read_text().splitlines()), values)


if __name__ == "__main__":
    unittest.main()
