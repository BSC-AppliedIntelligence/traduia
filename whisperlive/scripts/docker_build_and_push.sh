#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE_NAME="${IMAGE_NAME:-ghcr.io/bsc-appliedintelligence/traduia/whisperlive:latest}"
DOCKERFILE_PATH="${DOCKERFILE_PATH:-${REPO_ROOT}/docker/Dockerfile.gpu.spark.ct2-source}"
BUILD_CONTEXT="${BUILD_CONTEXT:-${REPO_ROOT}}"
PLATFORMS="${PLATFORMS:-linux/arm64}"
PUSH="${PUSH:-false}"
LOAD="${LOAD:-false}"
OUTPUT_PATH="${OUTPUT_PATH:-}"
DOCKER_LOGIN="${DOCKER_LOGIN:-false}"
REGISTRY="${REGISTRY:-ghcr.io}"
PYTORCH_INDEX_URL="${PYTORCH_INDEX_URL:-https://download.pytorch.org/whl/cu128}"
TORCH_PACKAGE="${TORCH_PACKAGE:-torch}"
TORCHAUDIO_PACKAGE="${TORCHAUDIO_PACKAGE:-torchaudio}"
CTRANSLATE2_PACKAGE="${CTRANSLATE2_PACKAGE:-ctranslate2}"
CTRANSLATE2_VERSION="${CTRANSLATE2_VERSION:-v4.7.1}"
CTRANSLATE2_INSTALL_PREFIX="${CTRANSLATE2_INSTALL_PREFIX:-/opt/ctranslate2}"
NUMPY_PACKAGE="${NUMPY_PACKAGE:-numpy<2}"
BUILD_ACTION="archive"

if [[ ! -f "${DOCKERFILE_PATH}" ]]; then
  echo "Dockerfile not found: ${DOCKERFILE_PATH}" >&2
  exit 1
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
  --build-arg "PYTORCH_INDEX_URL=${PYTORCH_INDEX_URL}"
  --build-arg "TORCH_PACKAGE=${TORCH_PACKAGE}"
  --build-arg "TORCHAUDIO_PACKAGE=${TORCHAUDIO_PACKAGE}"
  --build-arg "CTRANSLATE2_PACKAGE=${CTRANSLATE2_PACKAGE}"
  --build-arg "CTRANSLATE2_VERSION=${CTRANSLATE2_VERSION}"
  --build-arg "CTRANSLATE2_INSTALL_PREFIX=${CTRANSLATE2_INSTALL_PREFIX}"
  --build-arg "NUMPY_PACKAGE=${NUMPY_PACKAGE}"
)

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
echo -e "\tBUILD_CONTEXT=${BUILD_CONTEXT}"
echo -e "\tPLATFORMS=${PLATFORMS}"
echo -e "\tPUSH=${PUSH}"
echo -e "\tLOAD=${LOAD}"
echo -e "\tBUILD_ACTION=${BUILD_ACTION}"
echo -e "\tOUTPUT_PATH=${OUTPUT_PATH:-<none>}"
echo -e "\tDOCKER_LOGIN=${DOCKER_LOGIN}"
echo -e "\tREGISTRY=${REGISTRY}"
echo -e "\tPYTORCH_INDEX_URL=${PYTORCH_INDEX_URL}"
echo -e "\tTORCH_PACKAGE=${TORCH_PACKAGE}"
echo -e "\tTORCHAUDIO_PACKAGE=${TORCHAUDIO_PACKAGE}"
echo -e "\tCTRANSLATE2_PACKAGE=${CTRANSLATE2_PACKAGE}"
echo -e "\tCTRANSLATE2_VERSION=${CTRANSLATE2_VERSION}"
echo -e "\tCTRANSLATE2_INSTALL_PREFIX=${CTRANSLATE2_INSTALL_PREFIX}"
echo -e "\tNUMPY_PACKAGE=${NUMPY_PACKAGE}"
echo "###################################################################"

echo "Building image '${IMAGE_NAME}' using ${DOCKERFILE_PATH}"
echo "Context: ${BUILD_CONTEXT}"
echo "Platforms: ${PLATFORMS}"
docker "${build_args[@]}" "${BUILD_CONTEXT}"

if [[ "${PUSH}" == "true" ]]; then
  echo "Pushed image '${IMAGE_NAME}'."
elif [[ -n "${OUTPUT_PATH}" ]]; then
  echo "Wrote OCI image archive to ${OUTPUT_PATH}"
fi
echo "Done."
