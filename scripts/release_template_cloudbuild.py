#!/usr/bin/env python3
"""Stage a versioned Flex Template release without Docker or gcloud.

Docker-less twin of ``release-template.sh``: Cloud Build builds and pushes the
launcher image from a source tarball (``pipelines/Dockerfile`` + the shadow
JAR), then the template spec JSON is written exactly as
``gcloud dataflow flex-template build`` would write it. Needs only
Application Default Credentials with Cloud Build + Artifact Registry + GCS
permissions on the hosting project — it runs from a bare container or CI.

Usage:
    scripts/release_template_cloudbuild.py <version>          # e.g. 0.1.0a2

Build the JAR first (``cd pipelines && ./gradlew shadowJar``). The same
environment overrides as ``release-template.sh`` apply
(``DATENSEE_TEMPLATE_PROJECT``, ``_REGION``, ``_REPO``, ``_BUCKET``).
"""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import time
from pathlib import Path

import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage

_TERMINAL = {"SUCCESS", "FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED"}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"Usage: {argv[0]} <version>", file=sys.stderr)
        return 64
    version = argv[1]
    project = os.environ.get("DATENSEE_TEMPLATE_PROJECT", "datensee-testing")
    region = os.environ.get("DATENSEE_TEMPLATE_REGION", "us-central1")
    repo = os.environ.get("DATENSEE_TEMPLATE_REPO", "templates")
    bucket = os.environ.get("DATENSEE_TEMPLATE_BUCKET", "datensee-templates")
    image = f"{region}-docker.pkg.dev/{project}/{repo}/datensee-pipeline:{version}"
    source_bucket = f"{project}_cloudbuild"
    spec_path = f"v{version}/datensee.json"

    pipelines = Path(__file__).resolve().parents[1] / "pipelines"
    jar = pipelines / "build" / "libs" / "datensee-pipeline.jar"
    if not jar.exists():
        print(
            f"JAR not found: {jar}\nRun `cd pipelines && ./gradlew shadowJar` first.",
            file=sys.stderr,
        )
        return 1

    print(f"Version : {version}\nImage   : {image}\nSpec    : gs://{bucket}/{spec_path}")

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    gcs = storage.Client(project=project, credentials=credentials)
    session = AuthorizedSession(credentials)

    # [1/3] Source tarball laid out the way pipelines/Dockerfile expects.
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        archive.add(pipelines / "Dockerfile", arcname="Dockerfile")
        archive.add(jar, arcname="build/libs/datensee-pipeline.jar")
    source_object = f"source/datensee-pipeline-{version}-{int(time.time())}.tgz"
    print(
        f"[1/3] uploading source ({payload.tell() / 1e6:.0f} MB) → gs://{source_bucket}/{source_object}"
    )
    payload.seek(0)
    gcs.bucket(source_bucket).blob(source_object).upload_from_file(
        payload, content_type="application/gzip", timeout=600
    )

    # [2/3] Cloud Build: docker build + push.
    build = {
        "source": {"storageSource": {"bucket": source_bucket, "object": source_object}},
        "steps": [
            {
                "name": "gcr.io/cloud-builders/docker",
                "args": ["build", "-t", image, "-f", "Dockerfile", "."],
            }
        ],
        "images": [image],
        "timeout": "1200s",
        "options": {"logging": "CLOUD_LOGGING_ONLY"},
    }
    response = session.post(
        f"https://cloudbuild.googleapis.com/v1/projects/{project}/builds", json=build
    )
    response.raise_for_status()
    build_id = response.json()["metadata"]["build"]["id"]
    print(f"[2/3] cloud build {build_id}")
    while True:
        status = session.get(
            f"https://cloudbuild.googleapis.com/v1/projects/{project}/builds/{build_id}"
        ).json()
        print(f"      {time.strftime('%H:%M:%S')} {status['status']}")
        if status["status"] in _TERMINAL:
            break
        time.sleep(20)
    if status["status"] != "SUCCESS":
        print(f"Build {status['status']}: {status.get('logUrl')}", file=sys.stderr)
        return 1

    # [3/3] Template spec — the same document `gcloud dataflow flex-template build` emits.
    spec = {
        "image": image,
        "metadata": json.loads((pipelines / "metadata.json").read_text()),
        "sdkInfo": {"language": "JAVA"},
    }
    gcs.bucket(bucket).blob(spec_path).upload_from_string(
        json.dumps(spec, indent=2), content_type="application/json"
    )
    print(f"[3/3] spec written: gs://{bucket}/{spec_path}")
    print(
        f"\nRelease complete. Override at runtime: DATENSEE_TEMPLATE_SPEC=gs://{bucket}/{spec_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
