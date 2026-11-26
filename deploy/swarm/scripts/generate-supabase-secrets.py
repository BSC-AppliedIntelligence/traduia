#!/usr/bin/env python3
"""Generate the secrets of the self-hosted Supabase stack (swarm/supabase) and the values the
application stack needs from it.

Writes a private (mode 600) env file and refuses to overwrite an existing file. Secret values are never
printed; only the variable names are. Standard library only.

    python3 scripts/generate-supabase-secrets.py --output supabase-secrets.env

Output variables:
    POSTGRES_PASSWORD, JWT_SECRET            -> supabase stack
    ANON_KEY, SERVICE_ROLE_KEY               -> HS256 JWTs signed with JWT_SECRET (roles anon / service_role)
    SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY  -> same values under the names the application stack expects
Add SUPABASE_URL (https://supabase.<TRADUIA_DOMAIN>) yourself.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(secret: str, role: str, now: int | None = None, years: int = 10) -> str:
    """HS256 JWT in the shape Supabase expects (claims: role, iss=supabase, iat, exp)."""
    now = int(time.time()) if now is None else now
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps({"role": role, "iss": "supabase", "iat": now,
                               "exp": now + years * 365 * 86400}, separators=(",", ":")).encode())
    signature = _b64(hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def generate(now: int | None = None) -> dict:
    jwt_secret = secrets.token_hex(32)
    anon, service = make_jwt(jwt_secret, "anon", now), make_jwt(jwt_secret, "service_role", now)
    return {
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "JWT_SECRET": jwt_secret,
        "ANON_KEY": anon,
        "SERVICE_ROLE_KEY": service,
        "SUPABASE_ANON_KEY": anon,
        "SUPABASE_SERVICE_KEY": service,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", required=True, help="env file to create (must not exist)")
    args = parser.parse_args(argv)
    values = generate()
    try:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"refusing to overwrite existing file: {args.output}", file=sys.stderr)
        return 1
    with os.fdopen(fd, "w") as handle:
        handle.writelines(f"{name}={value}\n" for name, value in values.items())
    print(f"wrote {args.output} (mode 600) with: {', '.join(values)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
