"""Application Default Credentials helpers.

Long-lived callers (e.g. the Dataflow poll loop) should hold the
:class:`~google.auth.credentials.Credentials` object from
:func:`get_credentials` and let it refresh as tokens expire.
:func:`get_access_token` returns a one-shot bearer token for short-lived
calls. The Java pipeline handles its own auth independently via the
same ADC mechanism.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import google.auth
import google.auth.credentials
import google.auth.transport.requests

if TYPE_CHECKING:
    from google.cloud import storage

_EE_SCOPE = "https://www.googleapis.com/auth/earthengine"
_CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def get_credentials() -> google.auth.credentials.Credentials:
    """Return Application Default Credentials with cloud-platform scope.

    The credential object is refreshable: callers holding it across a
    token lifetime (~1 h) can call ``credentials.refresh(Request())``
    (or rely on consumers like :func:`datensee.status.poll_job` that
    refresh automatically) instead of restarting with a new token.

    Returns:
        Refreshable Google credentials for GCP APIs (Dataflow, GCS, ...).

    Raises:
        google.auth.exceptions.DefaultCredentialsError: If ADC is not configured.
            Run `gcloud auth application-default login` to fix this.
    """
    credentials, _ = google.auth.default(scopes=[_CLOUD_SCOPE])
    return credentials


def get_access_token(scopes: list[str] | None = None) -> str:
    """Return a fresh OAuth2 bearer token from Application Default Credentials.

    Args:
        scopes: OAuth2 scopes to request. Defaults to EE + Cloud Platform.

    Returns:
        Access token string suitable for an Authorization: Bearer header.

    Raises:
        google.auth.exceptions.DefaultCredentialsError: If ADC is not configured.
            Run `gcloud auth application-default login` to fix this.
    """
    if scopes is None:
        scopes = [_EE_SCOPE, _CLOUD_SCOPE]

    credentials, _ = google.auth.default(scopes=scopes)
    request = google.auth.transport.requests.Request()
    credentials.refresh(request)
    return credentials.token


def gcs_client(
    credentials: google.auth.credentials.Credentials | None = None,
    project: str | None = None,
) -> storage.Client:
    """Build a GCS client that never relies on ambient project inference.

    ``storage.Client()`` with no project falls back to the gcloud SDK's
    ``core/project`` setting — which a host with only ADC (``pip install
    datensee``, no gcloud) does not have, so it raises "Project was not
    passed and could not be determined". Object reads and writes don't
    need a project at all; we resolve one only for quota attribution.

    Resolution order: explicit ``project`` → the credentials' quota
    project (``gcloud auth application-default set-quota-project``) →
    ``None`` (an explicit ``None`` opts out of inference).
    """
    from google.cloud import storage

    resolved = project or getattr(credentials, "quota_project_id", None)
    return storage.Client(project=resolved, credentials=credentials)
