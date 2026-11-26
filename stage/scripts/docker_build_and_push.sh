#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE_NAME="${IMAGE_NAME:-ghcr.io/bsc-appliedintelligence/traduia/stage:latest}"
DOCKERFILE_PATH="${DOCKERFILE_PATH:-${REPO_ROOT}/Dockerfile}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
PUSH="${PUSH:-false}"
LOAD="${LOAD:-false}"
OUTPUT_PATH="${OUTPUT_PATH:-}"
DOCKER_LOGIN="${DOCKER_LOGIN:-false}"
REGISTRY="${REGISTRY:-ghcr.io}"

if [[ "${PUSH}" == "true" && "${DOCKER_LOGIN}" == "true" ]]; then
  echo "Logging in to ${REGISTRY}"
  if [[ -n "${REGISTRY_USER:-}" && -n "${REGISTRY_PASSWORD:-}" ]]; then
    printf '%s\n' "${REGISTRY_PASSWORD}" | docker login "${REGISTRY}" \
      --username "${REGISTRY_USER}" \
      --password-stdin
  else
    docker login "${REGISTRY}"
  fi
fi

build_args=(
  buildx build
  --platform "${PLATFORMS}"
  -f "${DOCKERFILE_PATH}"
  -t "${IMAGE_NAME}"
)

if [[ "${PUSH}" == "true" ]]; then
  build_args+=(--push)
elif [[ "${LOAD}" == "true" ]]; then
  if [[ "${PLATFORMS}" == *,* ]]; then
    echo "LOAD=true only supports a single platform. Current PLATFORMS=${PLATFORMS}" >&2
    echo "Use PUSH=true for a multi-platform registry image, or set PLATFORMS to one platform." >&2
    exit 1
  fi
  build_args+=(--load)
else
  if [[ "${PLATFORMS}" == *,* ]]; then
    safe_image_name="${IMAGE_NAME//[\/:]/_}"
    OUTPUT_PATH="${OUTPUT_PATH:-${REPO_ROOT}/dist/${safe_image_name}.oci.tar}"
    mkdir -p "$(dirname "${OUTPUT_PATH}")"
    build_args+=(--output "type=oci,dest=${OUTPUT_PATH}")
  else
    build_args+=(--load)
  fi
fi

echo "Building image '${IMAGE_NAME}' using ${DOCKERFILE_PATH}"
echo "Platforms: ${PLATFORMS}"
docker "${build_args[@]}" "${REPO_ROOT}"

if [[ "${PUSH}" == "true" ]]; then
  echo "Pushed multi-platform image '${IMAGE_NAME}'."
elif [[ "${OUTPUT_PATH}" != "" ]]; then
  echo "Wrote OCI image archive to ${OUTPUT_PATH}"
fi
echo "Done."
