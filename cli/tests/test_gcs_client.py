"""``auth.gcs_client`` / ``auth.split_gcs_uri`` — GCS access without ambient project inference.

Regression: on a host with ADC but no gcloud SDK config (the ``pip install
datensee`` path), ``storage.Client()`` raised "Project was not passed and
could not be determined from the environment" before the first upload.
The fix is passing ``project=None`` *explicitly* — omitting it is what
triggers the inference.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.auth.credentials import AnonymousCredentials

from datensee import auth


@pytest.fixture
def captured_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace ``storage.Client`` with a recorder and return its kwargs."""
    from google.cloud import storage

    seen: dict[str, object] = {}

    def fake_client(**kwargs: object) -> object:
        seen.update(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(storage, "Client", fake_client)
    return seen


def test_passes_explicit_none_project(captured_client: dict[str, object]) -> None:
    creds = AnonymousCredentials()
    auth.gcs_client(creds)
    assert captured_client == {"project": None, "credentials": creds}


def test_no_credentials_still_opts_out_of_inference(captured_client: dict[str, object]) -> None:
    auth.gcs_client()
    assert "project" in captured_client and captured_client["project"] is None
    assert captured_client["credentials"] is None


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("gs://bucket/exports/run", ("bucket", "exports/run")),
        ("gs://bucket/exports/run/", ("bucket", "exports/run/")),
        ("gs://bucket", ("bucket", "")),
    ],
)
def test_split_gcs_uri(uri: str, expected: tuple[str, str]) -> None:
    assert auth.split_gcs_uri(uri) == expected


def test_split_gcs_uri_rejects_non_gcs() -> None:
    with pytest.raises(ValueError, match="Not a gs:// URI"):
        auth.split_gcs_uri("/local/path")
