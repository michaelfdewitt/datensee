"""Smoke tests for the FastAPI /submit endpoint.

We stub `datensee.api.export` so the tests exercise the service's own
logic — request parsing, access_token handling, response shaping, error
classification — without spawning Java or touching Earth Engine.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from datensee_service import app as app_module


@pytest.fixture
def client() -> TestClient:
    return TestClient(app_module.app)


@pytest.fixture
def fake_export_result() -> MagicMock:
    """Shape that looks like datensee's ExportResult for happy-path responses."""
    result = MagicMock()
    result.job_id = "2026-04-15_fake-job"
    result.config.tile_count = 4
    result.config.output.output_path = "gs://my-bucket/prefix/"
    result.config.tile_grid.scale_meters = 30.0
    result.config.tile_grid.crs = "EPSG:4326"
    result.config.runner.dataflow.labels = {"foundree": "1"}
    return result


def _body(**overrides: Any) -> dict[str, Any]:
    base = {
        "expression": '{"fake":"expr"}',
        "region": {
            "type": "Polygon",
            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
        },
        "project": "user-project",
        "output": "gs://my-bucket/prefix/",
        "access_token": "ya29.test-token",
        "dry_run": True,
    }
    base.update(overrides)
    return base


def test_submit_happy_path_forwards_to_datensee(
    client: TestClient, fake_export_result: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_export(**kwargs: Any) -> MagicMock:
        calls.append(kwargs)
        return fake_export_result

    from datensee import api as datensee_api

    monkeypatch.setattr(datensee_api, "export", fake_export)

    resp = client.post("/submit", json=_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["job_id"] == "2026-04-15_fake-job"
    assert body["tile_count"] == 4
    assert body["output"] == "gs://my-bucket/prefix/"

    assert len(calls) == 1
    call = calls[0]
    assert call["project"] == "user-project"
    assert call["runner"] == "dataflow"
    # Credentials object should have been built from the access_token
    assert call["credentials"] is not None
    assert call["credentials"].token == "ya29.test-token"


def test_submit_refuses_real_submit_without_access_token(client: TestClient) -> None:
    resp = client.post("/submit", json=_body(access_token=None, dry_run=False))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "auth"


def test_submit_allows_dry_run_without_access_token(
    client: TestClient, fake_export_result: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datensee import api as datensee_api

    monkeypatch.setattr(datensee_api, "export", lambda **_: fake_export_result)

    resp = client.post("/submit", json=_body(access_token=None, dry_run=True))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_submit_surfaces_validation_errors_as_ok_false(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datensee import api as datensee_api

    def boom(**_: Any) -> None:
        raise ValueError("bad geometry")

    monkeypatch.setattr(datensee_api, "export", boom)

    resp = client.post("/submit", json=_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "validation"
    assert body["error"] == "bad geometry"


def test_submit_surfaces_runtime_errors_as_ok_false(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datensee import api as datensee_api

    def boom(**_: Any) -> None:
        raise RuntimeError("dataflow submit blew up")

    monkeypatch.setattr(datensee_api, "export", boom)

    resp = client.post("/submit", json=_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "runtime"


def test_scrub_never_includes_access_token(
    fake_export_result: MagicMock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Defense in depth: even if the request is logged, the token must never appear."""
    from datensee import api as datensee_api

    monkeypatch.setattr(datensee_api, "export", lambda **_: fake_export_result)

    client = TestClient(app_module.app)
    with caplog.at_level("INFO", logger="datensee.service"):
        client.post("/submit", json=_body(access_token="ya29.SECRET-SHOULD-NOT-LEAK"))

    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert "ya29.SECRET-SHOULD-NOT-LEAK" not in combined


def test_health_reports_datensee_version(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "datensee_version" in body
