#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE_NAME="${IMAGE_NAME:-ghcr.io/bsc-appliedintelligence/traduia/backstage:latest}"
DOCKERFILE_PATH="${DOCKERFILE_PATH:-${REPO_ROOT}/Dockerfile}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
PUSH="${PUSH:-false}"
LOAD="${LOAD:-false}"
OUTPUT_PATH="${OUTPUT_PATH:-}"
DOCKER_LOGIN="${DOCKER_LOGIN:-false}"
REGISTRY="${REGISTRY:-ghcr.io}"
BUILDER_NAME="${BUILDER_NAME:-whisperlive-builder}"
BUILDER_NETWORK="${BUILDER_NETWORK:-host}"
BUILD_NETWORK="${BUILD_NETWORK:-host}"
BUILD_ACTION="archive"

if [[ -n "${BUILDER_NAME}" ]]; then
  if ! docker buildx inspect "${BUILDER_NAME}" >/dev/null 2>&1; then
    echo "Creating buildx builder '${BUILDER_NAME}' with network=${BUILDER_NETWORK}"
    docker buildx create \
      --name "${BUILDER_NAME}" \
      --use \
      --driver docker-container \
      --driver-opt "network=${BUILDER_NETWORK}" >/dev/null
  else
    docker buildx use "${BUILDER_NAME}"
  fi
  docker buildx inspect "${BUILDER_NAME}" --bootstrap >/dev/null
fi

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

if [[ -n "${BUILD_NETWORK}" ]]; then
  build_args+=(--network "${BUILD_NETWORK}")
fi

if [[ "${PUSH}" == "true" ]]; then
  BUILD_ACTION="push"
  build_args+=(--push)
elif [[ "${LOAD}" == "true" ]]; then
  if [[ "${PLATFORMS}" == *,* ]]; then
    echo "LOAD=true only supports a single platform. Current PLATFORMS=${PLATFORMS}" >&2
    echo "Use PUSH=true for a multi-platform registry image, or set PLATFORMS to one platform." >&2
    exit 1
  fi
  BUILD_ACTION="load"
  build_args+=(--load)
else
  if [[ "${PLATFORMS}" == *,* ]]; then
    safe_image_name="${IMAGE_NAME//[\/:]/_}"
    OUTPUT_PATH="${OUTPUT_PATH:-${REPO_ROOT}/dist/${safe_image_name}.oci.tar}"
    mkdir -p "$(dirname "${OUTPUT_PATH}")"
    build_args+=(--output "type=oci,dest=${OUTPUT_PATH}")
  else
    BUILD_ACTION="load"
    build_args+=(--load)
  fi
fi

echo "###################################################################"
echo "Configuration:"
echo -e "\tIMAGE_NAME=${IMAGE_NAME}"
echo -e "\tDOCKERFILE_PATH=${DOCKERFILE_PATH}"
echo -e "\tPLATFORMS=${PLATFORMS}"
echo -e "\tPUSH=${PUSH}"
echo -e "\tLOAD=${LOAD}"
echo -e "\tBUILD_ACTION=${BUILD_ACTION}"
echo -e "\tOUTPUT_PATH=${OUTPUT_PATH:-<none>}"
echo -e "\tDOCKER_LOGIN=${DOCKER_LOGIN}"
echo -e "\tREGISTRY=${REGISTRY}"
echo -e "\tBUILDER_NAME=${BUILDER_NAME:-<default>}"
echo -e "\tBUILDER_NETWORK=${BUILDER_NETWORK}"
echo -e "\tBUILD_NETWORK=${BUILD_NETWORK:-<default>}"
echo "###################################################################"

echo "Building image '${IMAGE_NAME}' using ${DOCKERFILE_PATH}"
echo "Platforms: ${PLATFORMS}"
docker "${build_args[@]}" "${REPO_ROOT}"

if [[ "${PUSH}" == "true" ]]; then
  echo "Pushed multi-platform image '${IMAGE_NAME}'."
elif [[ "${OUTPUT_PATH}" != "" ]]; then
  echo "Wrote OCI image archive to ${OUTPUT_PATH}"
fi
echo "Done."
