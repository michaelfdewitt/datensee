"""Application Default Credentials helper.

Retrieves an OAuth2 bearer token using ADC. This token is used by the
Python CLI for Dataflow job status polling. The Java pipeline handles
its own auth independently via the same ADC mechanism.
"""

from __future__ import annotations

import google.auth
import google.auth.transport.requests

_EE_SCOPE = "https://www.googleapis.com/auth/earthengine"
_CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


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
