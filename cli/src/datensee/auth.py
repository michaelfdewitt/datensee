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
) -> storage.Client:
    """Build a GCS client that never relies on ambient project inference.

    ``storage.Client()`` with the ``project`` argument omitted falls back
    to the gcloud SDK's ``core/project`` configuration. Environments with
    only ADC (such as pip-only installations without the gcloud CLI) lack
    this setting and raise "Project was not passed and could not be determined".
    Passing ``project=None`` explicitly opts out of project inference.
    Object reads and writes require no project argument; quota attribution
    derives from the credentials' quota project (``x-goog-user-project``).

    Args:
        credentials: Credentials to sign requests with. ``None`` resolves
            Application Default Credentials inside the client.

    Returns:
        A ``storage.Client`` with no default project.
    """
    from google.cloud import storage

    return storage.Client(project=None, credentials=credentials)


def split_gcs_uri(uri: str) -> tuple[str, str]:
    """Split ``gs://bucket/object/path`` into ``(bucket, object_path)``.

    Args:
        uri: A ``gs://`` URI. The object path may be empty (bucket root)
            and keeps any trailing slash the caller passed.

    Returns:
        The bucket name and the object path (without the leading slash).

    Raises:
        ValueError: If ``uri`` does not start with ``gs://``.
    """
    if not uri.startswith("gs://"):
        raise ValueError(f"Not a gs:// URI: {uri!r}")
    bucket, _, object_path = uri[len("gs://") :].partition("/")
    return bucket, object_path
