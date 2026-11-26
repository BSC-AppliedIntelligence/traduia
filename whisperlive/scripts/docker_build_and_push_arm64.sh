#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

export IMAGE_NAME="${IMAGE_NAME:-ghcr.io/bsc-appliedintelligence/traduia/whisperlive:arm64}"
export DOCKERFILE_PATH="${DOCKERFILE_PATH:-${REPO_ROOT}/docker/Dockerfile.gpu.spark.ct2-source}"
export PLATFORMS="${PLATFORMS:-linux/arm64}"
export PUSH="${PUSH:-true}"

bash "${SCRIPT_DIR}/docker_build_and_push.sh"
