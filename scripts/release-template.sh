#!/usr/bin/env bash
#
# Build, push, and stage a versioned Flex Template release.
#
# Usage: scripts/release-template.sh <version>
#   e.g. scripts/release-template.sh 0.1.0a1
#
# The version must match the [project] version in cli/pyproject.toml — the
# Python client pins itself to gs://${BUCKET}/v${VERSION}/datensee.json by
# default (see cli/src/datensee/template.py).

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <version>" >&2
    exit 64
fi

VERSION="$1"

PROJECT="${DATENSEE_TEMPLATE_PROJECT:-datensee-testing}"
REGION="${DATENSEE_TEMPLATE_REGION:-us-central1}"
REPO="${DATENSEE_TEMPLATE_REPO:-templates}"
BUCKET="${DATENSEE_TEMPLATE_BUCKET:-datensee-templates}"

REGISTRY="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}"
IMAGE="${REGISTRY}/datensee-pipeline:${VERSION}"
SPEC_GCS="gs://${BUCKET}/v${VERSION}/datensee.json"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIPELINES_DIR="${ROOT}/pipelines"

echo "Version : ${VERSION}"
echo "Image   : ${IMAGE}"
echo "Spec    : ${SPEC_GCS}"

cd "${PIPELINES_DIR}"

echo
echo "[1/4] gradle shadowJar"
./gradlew shadowJar

echo
echo "[2/4] docker build"
docker build -t "${IMAGE}" -f Dockerfile .

echo
echo "[3/4] docker push"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker push "${IMAGE}"

echo
echo "[4/4] gcloud dataflow flex-template build"
gcloud dataflow flex-template build "${SPEC_GCS}" \
    --image="${IMAGE}" \
    --sdk-language=JAVA \
    --metadata-file=metadata.json \
    --project="${PROJECT}"

echo
echo "Release complete."
echo "  pip wheel pinned default : ${SPEC_GCS}"
echo "  override at runtime      : DATENSEE_TEMPLATE_SPEC=gs://... datensee export ..."
