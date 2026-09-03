"""Flex Template spec resolution for Dataflow submission.

The pipeline JAR lives inside a Flex Template container in Artifact
Registry; the launch endpoint takes a GCS URI pointing at the template
spec JSON. This module derives the default spec URI from the package
version so each datensee Python release launches a matching template
version, plus the override knob.

Pinning rule: ``scripts/release_template_cloudbuild.py <version>`` must have staged
``gs://datensee-templates/v<version>/datensee.json`` before the wheel
for ``<version>`` is published; the wheel has no fallback.
"""

from __future__ import annotations

import os

from datensee._version import __version__

TEMPLATE_BUCKET = "datensee-templates"

DEFAULT_TEMPLATE_SPEC_GCS_URI = f"gs://{TEMPLATE_BUCKET}/v{__version__}/datensee.json"

TEMPLATE_SPEC_ENV_VAR = "DATENSEE_TEMPLATE_SPEC"


def resolve_template_spec(override: str | None = None) -> str:
    """Return the Flex Template spec GCS URI to launch against.

    Priority: explicit override > ``DATENSEE_TEMPLATE_SPEC`` env var >
    package default (pinned to this package's version).
    """
    if override:
        return override
    env_value = os.environ.get(TEMPLATE_SPEC_ENV_VAR)
    if env_value:
        return env_value
    return DEFAULT_TEMPLATE_SPEC_GCS_URI
