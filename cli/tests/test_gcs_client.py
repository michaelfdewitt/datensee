"""``auth.gcs_client`` must never depend on ambient project inference.

Regression: on a host with ADC but no gcloud SDK config (the ``pip install
datensee`` path), ``storage.Client()`` raised "Project was not passed and
could not be determined from the environment" before the first upload.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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


def test_explicit_project_wins(captured_client: dict[str, object]) -> None:
    creds = SimpleNamespace(quota_project_id="quota-proj")
    auth.gcs_client(creds, project="explicit-proj")  # type: ignore[arg-type]
    assert captured_client == {"project": "explicit-proj", "credentials": creds}


def test_falls_back_to_quota_project(captured_client: dict[str, object]) -> None:
    creds = SimpleNamespace(quota_project_id="quota-proj")
    auth.gcs_client(creds)  # type: ignore[arg-type]
    assert captured_client["project"] == "quota-proj"


def test_passes_explicit_none_to_opt_out_of_inference(
    captured_client: dict[str, object],
) -> None:
    """``project=None`` must be passed *explicitly* — omitting it triggers inference."""
    auth.gcs_client(SimpleNamespace())  # type: ignore[arg-type]
    assert "project" in captured_client
    assert captured_client["project"] is None
