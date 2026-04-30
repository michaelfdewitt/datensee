"""Flex Template spec resolution for Dataflow submission.

The pipeline JAR lives inside a Flex Template container in Artifact
Registry; the launch endpoint takes a GCS URI pointing at the template
spec JSON. This module owns the constant pinning each datensee Python
release to a matching template version, plus the override knob.

Pinning rule: the default spec URI's version segment must match the
package version in ``cli/pyproject.toml``. Bump them together.
"""

from __future__ import annotations

import os

DEFAULT_TEMPLATE_SPEC_GCS_URI = "gs://datensee-templates/v0.1.0a1/datensee.json"

TEMPLATE_SPEC_ENV_VAR = "DATENSEE_TEMPLATE_SPEC"


def resolve_template_spec(override: str | None = None) -> str:
    """Return the Flex Template spec GCS URI to launch against.

    Priority: explicit override > ``DATENSEE_TEMPLATE_SPEC`` env var >
    package default.
    """
    if override:
        return override
    env_value = os.environ.get(TEMPLATE_SPEC_ENV_VAR)
    if env_value:
        return env_value
    return DEFAULT_TEMPLATE_SPEC_GCS_URI
