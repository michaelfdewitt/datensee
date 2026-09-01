#!/usr/bin/env python3
"""Stage a versioned Flex Template release without Docker or gcloud.

Docker-less twin of ``release-template.sh``: Cloud Build builds and pushes the
launcher image from a source tarball (``pipelines/Dockerfile`` + the shadow
JAR), then the template spec JSON is written the way
``gcloud dataflow flex-template build`` writes it, and the JAR (+ .sha256
sidecar) is staged at ``gs://<bucket>/v<version>/`` — the public fallback
``datensee jar download`` uses when the GitHub Release is unreachable.
Needs only Application
Default Credentials with Cloud Build + Artifact Registry + GCS permissions on
the hosting project — it runs from a bare container or CI.

Usage:
    scripts/release_template_cloudbuild.py <version> [--allow-version-mismatch]

``<version>`` must match ``cli/pyproject.toml`` (the wheel pins itself to
``gs://<bucket>/v<version>/datensee.json``); pass the flag only for dev tags.
Build the JAR first (``cd pipelines && ./gradlew shadowJar``). The same
environment overrides as ``release-template.sh`` apply
(``DATENSEE_TEMPLATE_PROJECT``, ``_REGION``, ``_REPO``, ``_BUCKET``).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time
from pathlib import Path

import google.auth
import google.auth.transport.requests
import httpx
from google.auth.credentials import Credentials
from google.cloud import storage
from google.cloud.exceptions import NotFound

ROOT = Path(__file__).resolve().parents[1]
PIPELINES = ROOT / "pipelines"
JAR = PIPELINES / "build" / "libs" / "datensee-pipeline.jar"

_TERMINAL = {"SUCCESS", "FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED"}
_CLOUD_BUILD = "https://cloudbuild.googleapis.com/v1"


def _package_version() -> str:
    text = (ROOT / "cli" / "pyproject.toml").read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if match is None:
        raise SystemExit("could not find `version = ...` in cli/pyproject.toml")
    return match.group(1)


def _authorized(credentials: Credentials) -> httpx.Client:
    """httpx client that sends a fresh bearer token on every request."""

    def bearer(request: httpx.Request) -> httpx.Request:
        if not credentials.valid:
            credentials.refresh(google.auth.transport.requests.Request())
        request.headers["Authorization"] = f"Bearer {credentials.token}"
        return request

    return httpx.Client(auth=bearer, timeout=60)


def _source_tarball(version: str) -> io.BytesIO:
    """Dockerfile + JAR, laid out the way pipelines/Dockerfile expects.

    The JAR is already deflate-compressed, so gzip level 1 is as small as
    level 9 and several times faster.
    """
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz", compresslevel=1) as archive:
        archive.add(PIPELINES / "Dockerfile", arcname="Dockerfile")
        archive.add(JAR, arcname="build/libs/datensee-pipeline.jar")
    payload.seek(0)
    return payload


def _source_bucket(gcs: storage.Client, project: str, region: str) -> storage.Bucket:
    """`<project>_cloudbuild` — the bucket `gcloud builds submit` would create."""
    name = f"{project}_cloudbuild"
    try:
        return gcs.get_bucket(name)
    except NotFound:
        print(f"      creating gs://{name} ({region})")
        return gcs.create_bucket(name, location=region)


def _run_cloud_build(
    http: httpx.Client, project: str, build: dict[str, object]
) -> dict[str, object]:
    """Submit a build and poll it to a terminal state; returns the final Build resource."""
    response = http.post(f"{_CLOUD_BUILD}/projects/{project}/builds", json=build)
    response.raise_for_status()
    build_id = response.json()["metadata"]["build"]["id"]
    print(f"[2/3] cloud build {build_id}")
    while True:
        status = http.get(f"{_CLOUD_BUILD}/projects/{project}/builds/{build_id}")
        status.raise_for_status()
        current = status.json()
        print(f"      {time.strftime('%H:%M:%S')} {current['status']}")
        if current["status"] in _TERMINAL:
            return current
        time.sleep(20)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("version")
    parser.add_argument(
        "--allow-version-mismatch",
        action="store_true",
        help="Stage a tag that differs from cli/pyproject.toml (dev builds only).",
    )
    args = parser.parse_args(argv[1:])
    version: str = args.version

    packaged = _package_version()
    if version != packaged and not args.allow_version_mismatch:
        print(
            f"version {version!r} != cli/pyproject.toml {packaged!r}; the wheel pins to its own "
            "version. Bump pyproject first, or pass --allow-version-mismatch for a dev tag.",
            file=sys.stderr,
        )
        return 64
    if not JAR.exists():
        print(
            f"JAR not found: {JAR}\nRun `cd pipelines && ./gradlew shadowJar` first.",
            file=sys.stderr,
        )
        return 1

    project = os.environ.get("DATENSEE_TEMPLATE_PROJECT", "datensee-testing")
    region = os.environ.get("DATENSEE_TEMPLATE_REGION", "us-central1")
    repo = os.environ.get("DATENSEE_TEMPLATE_REPO", "templates")
    bucket = os.environ.get("DATENSEE_TEMPLATE_BUCKET", "datensee-templates")
    image = f"{region}-docker.pkg.dev/{project}/{repo}/datensee-pipeline:{version}"
    spec_path = f"v{version}/datensee.json"
    print(f"Version : {version}\nImage   : {image}\nSpec    : gs://{bucket}/{spec_path}")

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    gcs = storage.Client(project=project, credentials=credentials)

    payload = _source_tarball(version)
    source_object = f"source/datensee-pipeline-{version}-{int(time.time())}.tgz"
    print(f"[1/3] uploading source ({payload.getbuffer().nbytes / 1e6:.0f} MB) → {source_object}")
    source_bucket = _source_bucket(gcs, project, region)
    source_bucket.blob(source_object).upload_from_file(
        payload, content_type="application/gzip", timeout=600
    )

    with _authorized(credentials) as http:
        result = _run_cloud_build(
            http,
            project,
            {
                "source": {
                    "storageSource": {"bucket": source_bucket.name, "object": source_object}
                },
                "steps": [
                    {
                        "name": "gcr.io/cloud-builders/docker",
                        "args": ["build", "-t", image, "-f", "Dockerfile", "."],
                    }
                ],
                "images": [image],
                "timeout": "1200s",
                "options": {"logging": "CLOUD_LOGGING_ONLY"},
            },
        )
    if result["status"] != "SUCCESS":
        print(f"Build {result['status']}: {result.get('logUrl')}", file=sys.stderr)
        return 1

    spec = {
        "image": image,
        "metadata": json.loads((PIPELINES / "metadata.json").read_text()),
        "sdkInfo": {"language": "JAVA"},
        "defaultEnvironment": {},
    }
    gcs.bucket(bucket).blob(spec_path).upload_from_string(
        json.dumps(spec, indent=2), content_type="application/json"
    )
    print(f"[3/4] spec written: gs://{bucket}/{spec_path}")

    # The public download fallback for `datensee jar download`: versioned
    # path (a new release never touches an old prefix) + sha256 sidecar
    # (the CLI verifies it, so a mutated object is refused, not run).
    jar_sha = hashlib.sha256(JAR.read_bytes()).hexdigest()
    jar_blob = f"v{version}/{JAR.name}"
    gcs.bucket(bucket).blob(jar_blob).upload_from_filename(
        str(JAR), content_type="application/java-archive", timeout=600
    )
    gcs.bucket(bucket).blob(jar_blob + ".sha256").upload_from_string(
        jar_sha + "  " + JAR.name + "\n", content_type="text/plain"
    )
    print(f"[4/4] jar fallback staged: gs://{bucket}/{jar_blob} (sha256 {jar_sha[:12]}…)")
    print(
        f"\nRelease complete. Override at runtime: DATENSEE_TEMPLATE_SPEC=gs://{bucket}/{spec_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
