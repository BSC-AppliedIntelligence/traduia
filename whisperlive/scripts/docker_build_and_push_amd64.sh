#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

export IMAGE_NAME="${IMAGE_NAME:-ghcr.io/bsc-appliedintelligence/traduia/whisperlive:amd64}"
export DOCKERFILE_PATH="${DOCKERFILE_PATH:-${REPO_ROOT}/docker/Dockerfile.gpu}"
export PLATFORMS="${PLATFORMS:-linux/amd64}"
export PUSH="${PUSH:-true}"

bash "${SCRIPT_DIR}/docker_build_and_push.sh"
