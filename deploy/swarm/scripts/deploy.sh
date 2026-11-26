#!/usr/bin/env bash
# Deploy the TraduIA Swarm stack from an environment file (create one with init-env.py). Idempotent: run it again
# to update. It prepares the host (data directories, media-port firewall), validates the configuration,
# deploys the stack and waits until the services answer.
#
# Usage: deploy.sh <env-file> [--stack NAME] [--no-host-setup] [--no-wait]
#   --stack NAME      docker stack name (default: traduia)
#   --no-host-setup   do not create directories / install the firewall unit (you did it already or use Portainer)
#   --no-wait         return right after `docker stack deploy`
# Needs: Docker in Swarm mode (manager node), sudo for host setup.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SWARM_DIR="${SWARM_DIR:-$(cd "$HERE/.." && pwd)}"
STACK=traduia; HOST_SETUP=1; WAIT=1; ENV_FILE=""
WAIT_SECONDS="${WAIT_SECONDS:-900}"

die()  { echo "error: $*" >&2; exit 1; }
step() { echo "==> $*"; }
usage() { sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --stack) STACK="${2:?--stack needs a name}"; shift 2 ;;
    --no-host-setup) HOST_SETUP=0; shift ;;
    --no-wait) WAIT=0; shift ;;
    -h|--help) usage 0 ;;
    -*) die "unknown option $1 (see --help)" ;;
    *) [ -z "$ENV_FILE" ] || die "only one env file expected"; ENV_FILE="$1"; shift ;;
  esac
done
[ -n "$ENV_FILE" ] || usage 1
[ -f "$ENV_FILE" ] || die "env file not found: $ENV_FILE"
STACK_FILE="$SWARM_DIR/stack.yml"
[ -f "$STACK_FILE" ] || die "stack file not found: $STACK_FILE"

set -a; . "$ENV_FILE"; set +a

required=(TRADUIA_DOMAIN TRADUIA_PUBLIC_IP TRADUIA_NODE FORWARDED_ALLOW_IPS LIVEKIT_API_KEY LIVEKIT_API_SECRET
          SUPABASE_URL SUPABASE_ANON_KEY SUPABASE_SERVICE_KEY)
BUILTIN=0
if grep -q '^  supabase-db:' "$STACK_FILE"; then BUILTIN=1; required+=(POSTGRES_PASSWORD JWT_SECRET); fi
missing=(); for v in "${required[@]}"; do [ -n "${!v:-}" ] || missing+=("$v"); done
[ ${#missing[@]} -eq 0 ] || die "missing in $ENV_FILE: ${missing[*]}"

step "checking Docker Swarm"
[ "$(docker info --format '{{.Swarm.LocalNodeState}}')" = "active" ] || die "this node is not part of a swarm (docker swarm init)"
[ "$(docker info --format '{{.Swarm.ControlAvailable}}')" = "true" ] || die "run this on a swarm manager node"

DATA_DIR="${TRADUIA_DATA_DIR:-/data/traduia}"
if [ "$HOST_SETUP" = 1 ]; then
  step "preparing the host (directories, media-port firewall)"
  dirs=("$DATA_DIR/cache/whisperlive" "$DATA_DIR/cache/modelskit" "$DATA_DIR/recordings/backstage")
  mkdir -p "${dirs[@]}" 2>/dev/null || sudo mkdir -p "${dirs[@]}"
  if ! systemctl is-active --quiet livekit-media-fw.service; then
    fw="$SWARM_DIR/host/livekit-media-fw"
    sudo install -m 755 "$fw/livekit-media-fw.sh" /usr/local/sbin/livekit-media-fw.sh
    sudo install -m 644 "$fw/livekit-media-fw.service" /etc/systemd/system/livekit-media-fw.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now livekit-media-fw.service
    echo "    installed livekit-media-fw.service (media ports 7881/7882 blocked from the Internet)"
  else
    echo "    livekit-media-fw.service already active"
  fi
fi

step "validating the configuration"
err="$(mktemp)"; trap 'rm -f "$err"' EXIT
docker stack config -c "$STACK_FILE" >/dev/null 2>"$err" || { cat "$err" >&2; die "invalid configuration (see above)"; }

step "deploying stack '$STACK'"
docker stack deploy -c "$STACK_FILE" "$STACK"

if [ "$WAIT" = 1 ]; then
  step "waiting for the services (up to ${WAIT_SECONDS}s; the first start downloads models and requests certificates)"
  deadline=$((SECONDS + WAIT_SECONDS)); ready=0
  while [ "$SECONDS" -lt "$deadline" ]; do
    pending=0
    while read -r name replicas rest; do
      if [[ "$replicas $rest" =~ \(([0-9]+)/([0-9]+)\ completed\) ]]; then [ "${BASH_REMATCH[1]}" = "${BASH_REMATCH[2]}" ] || pending=1
      elif [[ "$replicas" =~ ^([0-9]+)/([0-9]+)$ ]]; then [ "${BASH_REMATCH[1]}" = "${BASH_REMATCH[2]}" ] || pending=1
      fi
    done < <(docker stack services "$STACK" --format '{{.Name}} {{.Replicas}}')
    if [ "$pending" = 0 ]; then
      healthy=1
      [ "$BUILTIN" = 0 ] || curl -fsS -o /dev/null --max-time 10 "https://supabase.$TRADUIA_DOMAIN/auth/v1/health" 2>/dev/null || healthy=0
      curl -fsS -o /dev/null --max-time 10 "https://backstage.$TRADUIA_DOMAIN/login" 2>/dev/null || healthy=0
      [ "$healthy" = 1 ] && { ready=1; break; }
    fi
    sleep 10
  done
  [ "$ready" = 1 ] || die "not ready after ${WAIT_SECONDS}s: check 'docker stack ps $STACK --no-trunc' and the troubleshooting table in swarm/README.md"
fi

echo
echo "TraduIA is deployed as stack '$STACK':"
echo "  application   https://backstage.$TRADUIA_DOMAIN/login"
[ "$BUILTIN" = 0 ] || echo "  supabase API  https://supabase.$TRADUIA_DOMAIN"
[ -z "${ADMIN_EMAILS:-}" ] || echo "  sign up with ${ADMIN_EMAILS%%,*} (administrator addresses: ADMIN_EMAILS), then create an event"
echo "  verify media  TRADUIA_DOMAIN=$TRADUIA_DOMAIN LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=... swarm/tests/turn-media-test/run.sh relay"
