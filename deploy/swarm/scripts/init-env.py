#!/usr/bin/env python3
"""Create the environment file of the Swarm stack (swarm/stack.yml) with freshly generated secrets.

    python3 swarm/scripts/init-env.py --domain example.org --public-ip 203.0.113.10 \\
        --admin-email you@example.org --output traduia.env

The file is created with mode 600 and never overwritten. Secret values are not printed, only the variable names.
Use it with `swarm/scripts/deploy.sh traduia.env`, `docker stack deploy` (after `set -a; . traduia.env; set +a`) or by
loading it into the Portainer stack environment.

Detected when not given: the swarm node name (`docker info`) and the Traefik subnet (`docker network inspect traefik-public`).
Built-in Supabase (default): generates its secrets and keys. `--existing-supabase`: you provide the URL and keys of an
existing Supabase (local or cloud) and the built-in variables are left out; remove the supabase-* services from stack.yml.
Standard library only.
"""
import argparse
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
# RFC 1918 and carrier-grade NAT ranges; documentation ranges (e.g. 203.0.113.0/24) are accepted so examples work.
NOT_PUBLIC = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")]
EMAIL = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


def load_generator():
    spec = importlib.util.spec_from_file_location("generate_supabase_secrets", HERE / "generate-supabase-secrets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def docker(*args):
    """Output of a docker command, or None when docker is unavailable or the command fails."""
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def detect_node():
    return docker("info", "--format", "{{.Name}}") or socket.gethostname()


def detect_proxy_subnet(network="traefik-public"):
    out = docker("network", "inspect", network, "--format", "{{json .IPAM.Config}}")
    try:
        return json.loads(out)[0]["Subnet"] if out else None
    except (ValueError, IndexError, KeyError, TypeError):
        return None


def build(args):
    """Return the ordered (name, value) pairs of the environment file."""
    domain = args.domain.lower().rstrip(".")
    if not DOMAIN.match(domain):
        raise SystemExit(f"--domain {args.domain!r} is not a valid domain name")
    try:
        ip = ipaddress.ip_address(args.public_ip)
    except ValueError:
        raise SystemExit(f"--public-ip {args.public_ip!r} is not an IP address")
    if ip.version != 4 or any(ip in net for net in NOT_PUBLIC) or ip.is_loopback or ip.is_link_local \
            or ip.is_multicast or ip.is_unspecified:
        raise SystemExit("--public-ip must be the public IPv4 address of the swarm node")
    for email in args.admin_email:
        if not EMAIL.match(email):
            raise SystemExit(f"--admin-email {email!r} is not an email address")
    subnet = args.forwarded_allow_ips or detect_proxy_subnet()
    if not subnet:
        raise SystemExit("cannot detect the Traefik subnet: pass --forwarded-allow-ips (docker network inspect traefik-public)")
    ipaddress.ip_network(subnet, strict=False)
    node = args.node or detect_node()

    values = [("TRADUIA_DOMAIN", domain), ("TRADUIA_PUBLIC_IP", str(ip)), ("TRADUIA_NODE", node),
              ("FORWARDED_ALLOW_IPS", subnet)]
    if args.data_dir:
        values.append(("TRADUIA_DATA_DIR", args.data_dir))
    if args.cert_resolver:
        values.append(("TRAEFIK_CERT_RESOLVER", args.cert_resolver))
    values += [("LIVEKIT_API_KEY", args.livekit_key), ("LIVEKIT_API_SECRET", secrets.token_hex(32))]

    if args.existing_supabase:
        missing = [flag for flag, value in (("--supabase-url", args.supabase_url), ("--supabase-anon-key", args.supabase_anon_key),
                                            ("--supabase-service-key", args.supabase_service_key)) if not value]
        if missing:
            raise SystemExit("--existing-supabase needs " + ", ".join(missing))
        if not re.match(r"^https?://[^/\s]+", args.supabase_url):
            raise SystemExit("--supabase-url must be a full http(s) URL")
        values += [("SUPABASE_URL", args.supabase_url.rstrip("/")), ("SUPABASE_ANON_KEY", args.supabase_anon_key),
                   ("SUPABASE_SERVICE_KEY", args.supabase_service_key)]
    else:
        generated = load_generator().generate()
        values += [("SUPABASE_URL", f"https://supabase.{domain}"), ("SUPABASE_ANON_KEY", generated["SUPABASE_ANON_KEY"]),
                   ("SUPABASE_SERVICE_KEY", generated["SUPABASE_SERVICE_KEY"]),
                   ("POSTGRES_PASSWORD", generated["POSTGRES_PASSWORD"]), ("JWT_SECRET", generated["JWT_SECRET"])]
        if args.init_image:
            values.append(("SUPABASE_INIT_IMAGE", args.init_image))
        if args.admin_email:
            values.append(("ADMIN_EMAILS", ",".join(args.admin_email)))
    return values


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", required=True, help="env file to create (must not exist)")
    parser.add_argument("--domain", required=True, help="base domain, e.g. example.org")
    parser.add_argument("--public-ip", required=True, help="public IPv4 of the swarm node")
    parser.add_argument("--node", help="swarm node hostname (default: detected)")
    parser.add_argument("--forwarded-allow-ips", help="Traefik subnet on traefik-public (default: detected)")
    parser.add_argument("--admin-email", action="append", default=[], metavar="EMAIL",
                        help="address that becomes an approved administrator at sign-up (repeatable; built-in Supabase)")
    parser.add_argument("--init-image", default=None,
                        help="database init image (default: the reference image pinned in stack.yml)")
    parser.add_argument("--existing-supabase", action="store_true", help="use an existing Supabase instead of the built-in one")
    parser.add_argument("--supabase-url")
    parser.add_argument("--supabase-anon-key")
    parser.add_argument("--supabase-service-key")
    parser.add_argument("--livekit-key", default="traduia", help="LiveKit API key name (default: traduia)")
    parser.add_argument("--data-dir", help="host data directory (default in stack.yml: /data/traduia)")
    parser.add_argument("--cert-resolver", help="Traefik certificate resolver (default in stack.yml: le)")
    args = parser.parse_args(argv)

    values = build(args)
    try:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(f"refusing to overwrite existing file: {args.output}")
    with os.fdopen(fd, "w") as handle:
        handle.writelines(f"{name}={value}\n" for name, value in values)
    print(f"wrote {args.output} (mode 600) with: {', '.join(name for name, _ in values)}")
    print("next: swarm/scripts/deploy.sh " + args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
