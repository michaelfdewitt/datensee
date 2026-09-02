#!/usr/bin/env bash
#
# One-time setup for the public Flex Template hosting under datensee-testing.
# Creates an Artifact Registry Docker repo for the launcher container and a
# GCS bucket for the template spec JSON, both readable by allUsers.
#
# Re-running is safe: each command tolerates pre-existing resources.

set -euo pipefail

PROJECT="${DATENSEE_TEMPLATE_PROJECT:-datensee-testing}"
REGION="${DATENSEE_TEMPLATE_REGION:-us-central1}"
REPO="${DATENSEE_TEMPLATE_REPO:-templates}"
BUCKET="${DATENSEE_TEMPLATE_BUCKET:-datensee-templates}"

echo "Project : ${PROJECT}"
echo "Region  : ${REGION}"
echo "Repo    : ${REPO}"
echo "Bucket  : gs://${BUCKET}"

gcloud artifacts repositories create "${REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --project="${PROJECT}" \
    --description="DatensEE Flex Template launcher images" \
    || echo "  (repo already exists, continuing)"

gcloud artifacts repositories add-iam-policy-binding "${REPO}" \
    --location="${REGION}" \
    --project="${PROJECT}" \
    --member="allUsers" \
    --role="roles/artifactregistry.reader"

gcloud storage buckets create "gs://${BUCKET}" \
    --project="${PROJECT}" \
    --location="${REGION}" \
    --uniform-bucket-level-access \
    || echo "  (bucket already exists, continuing)"

gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
    --member="allUsers" \
    --role="roles/storage.objectViewer"

echo
echo "Bootstrap complete."
echo "  image registry : ${REGION}-docker.pkg.dev/${PROJECT}/${REPO}"
echo "  spec bucket    : gs://${BUCKET}"
