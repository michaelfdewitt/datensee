"""Pinned SHA-256 of this release's pipeline JAR.

Written by the template-staging step (``scripts/release-template.sh`` /
``release_template_cloudbuild.py``), which builds the JAR once and uploads
it to the distribution bucket; committed alongside the version bump, so
the wheel published for a version carries the digest of exactly that
artifact. ``datensee.jar`` verifies every download — GitHub Release asset
and bucket fallback alike — against it, making the hosts pure
availability: a swapped or corrupted artifact anywhere fails loudly.

``None`` means "no pinned build" (development trees between releases);
downloads then fall back to the bucket's best-effort ``.sha256`` sidecar.
"""

from __future__ import annotations

# sha256 of datensee-pipeline.jar for the version in cli/pyproject.toml.
JAR_SHA256: str | None = "b8cf89f7dd667a5c03122919a18cd6da5bad8f8b179555b341f7a7c546f96c94"
