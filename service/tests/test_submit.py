"""Tests for the FastAPI service endpoints.

We stub `datensee.api.export` so the tests exercise the service's own
logic — request parsing, access_token handling, response shaping, error
classification, Spanner task-claim semantics — without spawning Java or
touching Earth Engine.

The export result fixture is a *real* `datensee.api.ExportResult` built
from a real `PipelineConfig`, so any attribute drift between datensee's
models and the service's response shaping fails these tests instead of
silently passing through a permissive MagicMock.

Spanner is replaced by an in-memory fake implementing the subset the
service uses: `run_in_transaction` (read + update inside one txn) and
`batch()` (update). The fake records every state write so tests can
assert on the exact state-transition history.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from datensee.api import ExportResult
from datensee.config import (
    AffineTransform,
    DataflowRunnerConfig,
    GridDimensions,
    OutputConfig,
    PipelineConfig,
    PixelGrid,
    RunnerConfig,
    TileCoordinate,
    TileGrid,
)
from datensee_service import app as app_module

SECRET_TOKEN = "ya29.SECRET-SHOULD-NOT-LEAK"  # noqa: S105 — test sentinel, not a credential
PIXEL_SIZE = 0.00027
USER_ID = "user-1"
TASK_ID = "task-1"


# ---------------------------------------------------------------------------
# Fixtures — real datensee models, in-memory Spanner fake
# ---------------------------------------------------------------------------


def _pipeline_config() -> PipelineConfig:
    """A real 2x2-tile dataflow-mode PipelineConfig (no mocks)."""
    return PipelineConfig(
        ee_expression='{"result":"0","values":{}}',
        gee_project="user-project",
        runner=RunnerConfig(
            mode="dataflow",
            dataflow=DataflowRunnerConfig(
                project="user-project",
                region="us-central1",
                temp_location="gs://my-bucket/prefix/_tmp",
                staging_location="gs://my-bucket/prefix/_staging",
                labels={"foundree": "1"},
            ),
        ),
        tile_grid=TileGrid(
            pixel_grid=PixelGrid(
                crs_code="EPSG:4326",
                affine_transform=AffineTransform(
                    scale_x=PIXEL_SIZE,
                    translate_x=0.0,
                    scale_y=-PIXEL_SIZE,
                    translate_y=1.0,
                ),
                dimensions=GridDimensions(width=1024, height=1024),
            ),
            tile_size_pixels=512,
            tiles=[
                TileCoordinate(
                    col_px=col * 512,
                    row_px=row * 512,
                    width_px=512,
                    height_px=512,
                    row=row,
                    col=col,
                )
                for row in range(2)
                for col in range(2)
            ],
        ),
        output=OutputConfig(output_path="gs://my-bucket/prefix/"),
    )


@pytest.fixture
def fake_export_result() -> ExportResult:
    """A real ExportResult so attribute drift in datensee models fails tests."""
    return ExportResult(config=_pipeline_config(), job_id="2026-04-15_fake-job")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app_module.app)


class _FakeTransaction:
    """Read/update view over the fake database (txn and batch share it)."""

    def __init__(self, db: FakeSpannerDatabase) -> None:
        self._db = db

    def read(self, *, table: str, columns: list[str], keyset: Any) -> Any:
        rows = []
        for key in keyset.keys:
            row = self._db.rows.get(tuple(key))
            if row is not None:
                rows.append([row[column] for column in columns])
        return iter(rows)

    def update(
        self, *, table: str, columns: list[str], values: list[list[Any]]
    ) -> None:
        self._db.apply_update(columns, values)


class _FakeBatch(_FakeTransaction):
    def __enter__(self) -> _FakeBatch:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class FakeSpannerDatabase:
    """In-memory stand-in for google.cloud.spanner Database.

    Implements `run_in_transaction` (the claim path) and `batch()` (the
    state-write path) with shared row storage, records every update in
    `update_log`, and raises on writes targeting any state listed in
    `fail_states` (to simulate Spanner outages mid-flight).
    """

    def __init__(
        self, rows: dict[tuple[str, str], dict[str, Any]] | None = None
    ) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = rows or {}
        self.update_log: list[dict[str, Any]] = []
        self.fail_states: set[str] = set()

    def apply_update(self, columns: list[str], values: list[list[Any]]) -> None:
        for row_values in values:
            record = dict(zip(columns, row_values, strict=True))
            if record.get("State") in self.fail_states:
                raise RuntimeError(
                    f"injected Spanner failure writing {record.get('State')}"
                )
            self.update_log.append(record)
            key = (record["UserId"], record["TaskId"])
            self.rows.setdefault(key, {}).update(record)

    def run_in_transaction(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(_FakeTransaction(self), *args, **kwargs)

    def batch(self) -> _FakeBatch:
        return _FakeBatch(self)

    def states_written(self) -> list[str]:
        return [record["State"] for record in self.update_log if "State" in record]


def _task_row(state: str = "PENDING") -> dict[str, Any]:
    payload = {
        "expression": '{"fake":"expr"}',
        "region": {
            "type": "Polygon",
            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
        },
        "project": "user-project",
        "output": "gs://my-bucket/prefix/",
        "dry_run": False,
    }
    return {
        "UserId": USER_ID,
        "TaskId": TASK_ID,
        "State": state,
        "Payload": json.dumps(payload),
    }


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> FakeSpannerDatabase:
    db = FakeSpannerDatabase({(USER_ID, TASK_ID): _task_row()})
    monkeypatch.setattr(app_module, "_get_spanner_db", lambda: db)
    return db


def _task_body() -> dict[str, Any]:
    return {"task_id": TASK_ID, "user_id": USER_ID, "access_token": "ya29.test-token"}


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


def _patch_export(
    monkeypatch: pytest.MonkeyPatch, result_or_exc: Any
) -> list[dict[str, Any]]:
    """Replace datensee.api.export; returns the recorded call-kwargs list."""
    from datensee import api as datensee_api

    calls: list[dict[str, Any]] = []

    def fake_export(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if isinstance(result_or_exc, Exception):
            raise result_or_exc
        return result_or_exc

    monkeypatch.setattr(datensee_api, "export", fake_export)
    return calls


# ---------------------------------------------------------------------------
# /submit
# ---------------------------------------------------------------------------


def test_submit_happy_path_forwards_to_datensee(
    client: TestClient,
    fake_export_result: ExportResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_export(monkeypatch, fake_export_result)

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


def test_submit_response_reports_pixel_size_crs_and_nominal_scale(
    client: TestClient,
    fake_export_result: ExportResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The response derives pixel_size/crs from the real TileGrid and echoes
    the request's nominal scale — tile_grid.scale_meters no longer exists."""
    _patch_export(monkeypatch, fake_export_result)

    resp = client.post("/submit", json=_body(scale=30.0))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["pixel_size"] == pytest.approx(PIXEL_SIZE)
    assert body["crs"] == "EPSG:4326"
    assert body["scale"] == 30.0
    assert body["labels"] == {"foundree": "1"}


def test_submit_response_shaping_never_500s_after_submission(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drifted/broken result object must degrade fields to null, not 500 —
    a 500 after submission makes the caller retry and duplicate the job."""

    class ExplodingConfig:
        def __getattr__(self, name: str) -> Any:
            raise AttributeError(f"simulated model drift: no attribute {name!r}")

    class BrokenResult:
        job_id = "2026-04-15_fake-job"
        config = ExplodingConfig()

    _patch_export(monkeypatch, BrokenResult())

    resp = client.post("/submit", json=_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["job_id"] == "2026-04-15_fake-job"
    assert body["tile_count"] is None
    assert body["pixel_size"] is None


def test_submit_refuses_real_submit_without_access_token(client: TestClient) -> None:
    resp = client.post("/submit", json=_body(access_token=None, dry_run=False))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "auth"


def test_submit_allows_dry_run_without_access_token(
    client: TestClient,
    fake_export_result: ExportResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_export(monkeypatch, fake_export_result)

    resp = client.post("/submit", json=_body(access_token=None, dry_run=True))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # dry_run is forwarded so export() itself skips the ensure_auth bootstrap;
    # the service must not monkeypatch datensee.notebook.
    assert calls[0]["dry_run"] is True
    assert calls[0]["credentials"] is None


def test_submit_surfaces_validation_errors_as_ok_false(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_export(monkeypatch, ValueError("bad geometry"))

    resp = client.post("/submit", json=_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "validation"
    assert body["error"] == "bad geometry"


def test_submit_surfaces_runtime_errors_as_ok_false(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_export(monkeypatch, RuntimeError("dataflow submit blew up"))

    resp = client.post("/submit", json=_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "runtime"


def test_submit_malformed_body_returns_400_with_scrubbed_detail(
    client: TestClient,
) -> None:
    """Non-task paths get 400 (not 422) and must never echo field values."""
    resp = client.post(
        "/submit",
        json=_body(access_token={"nested": SECRET_TOKEN}, region="not-a-dict"),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "validation"
    assert SECRET_TOKEN not in resp.text
    # Field locations are still named so the error is actionable.
    locs = {"/".join(entry["loc"]) for entry in body["detail"]}
    assert any("access_token" in loc for loc in locs)


def test_scrub_never_includes_access_token(
    fake_export_result: ExportResult,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defense in depth: even if the request is logged, the token must never appear."""
    _patch_export(monkeypatch, fake_export_result)

    client = TestClient(app_module.app)
    with caplog.at_level("INFO", logger="datensee.service"):
        client.post("/submit", json=_body(access_token=SECRET_TOKEN))

    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert SECRET_TOKEN not in combined


# ---------------------------------------------------------------------------
# /submit-task
# ---------------------------------------------------------------------------


def test_submit_task_success_claims_then_submits(
    client: TestClient,
    fake_db: FakeSpannerDatabase,
    fake_export_result: ExportResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_export(monkeypatch, fake_export_result)

    resp = client.post("/submit-task", json=_task_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["job_id"] == "2026-04-15_fake-job"
    assert body["tile_count"] == 4

    assert len(calls) == 1
    # Claim happened before the export, terminal state after it.
    assert fake_db.states_written() == ["RUNNING", "SUBMITTED"]
    row = fake_db.rows[(USER_ID, TASK_ID)]
    assert row["State"] == "SUBMITTED"
    assert row["DataflowJobId"] == "2026-04-15_fake-job"


def test_submit_task_second_delivery_is_skipped(
    client: TestClient,
    fake_db: FakeSpannerDatabase,
    fake_export_result: ExportResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At-least-once delivery: the second delivery must not re-submit."""
    calls = _patch_export(monkeypatch, fake_export_result)

    first = client.post("/submit-task", json=_task_body())
    second = client.post("/submit-task", json=_task_body())

    assert first.status_code == 200 and first.json()["ok"] is True
    assert second.status_code == 200
    assert second.json() == {"ok": True, "skipped": "already claimed"}
    assert len(calls) == 1  # exactly one Dataflow submission


@pytest.mark.parametrize("state", ["RUNNING", "SUBMITTED", "FAILED"])
def test_submit_task_non_pending_states_are_skipped_without_export(
    client: TestClient,
    fake_db: FakeSpannerDatabase,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    fake_db.rows[(USER_ID, TASK_ID)]["State"] = state
    calls = _patch_export(monkeypatch, AssertionError("export must not be called"))

    resp = client.post("/submit-task", json=_task_body())
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "skipped": "already claimed"}
    assert calls == []
    assert fake_db.update_log == []  # no state churn either


def test_submit_task_unknown_task_returns_ok_false(
    client: TestClient, fake_db: FakeSpannerDatabase
) -> None:
    resp = client.post(
        "/submit-task",
        json={"task_id": "no-such-task", "user_id": USER_ID, "access_token": "t"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "not_found"


def test_submit_task_spanner_write_failure_after_submit_still_200(
    client: TestClient,
    fake_db: FakeSpannerDatabase,
    fake_export_result: ExportResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job is already submitted — a Spanner outage on the SUBMITTED write
    must not surface as non-2xx, or Cloud Tasks retries and duplicates it."""
    fake_db.fail_states = {"SUBMITTED"}
    _patch_export(monkeypatch, fake_export_result)

    resp = client.post("/submit-task", json=_task_body())
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # Claim landed; terminal write was lost (row stuck in RUNNING, logged).
    assert fake_db.rows[(USER_ID, TASK_ID)]["State"] == "RUNNING"


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.TimeoutException("read timed out"),
        RuntimeError("Flex Template launch failed (HTTP 503): upstream unavailable"),
        RuntimeError("Flex Template launch failed (HTTP 429): quota exceeded"),
    ],
    ids=["connect-error", "timeout", "flex-503", "flex-429"],
)
def test_submit_task_transient_error_returns_503_and_resets_to_pending(
    client: TestClient,
    fake_db: FakeSpannerDatabase,
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
) -> None:
    _patch_export(monkeypatch, exc)

    resp = client.post("/submit-task", json=_task_body())
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "transient"

    assert fake_db.rows[(USER_ID, TASK_ID)]["State"] == "PENDING"
    assert "FAILED" not in fake_db.states_written()


def test_submit_task_transient_then_retry_succeeds(
    client: TestClient,
    fake_db: FakeSpannerDatabase,
    fake_export_result: ExportResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a transient 503 the task is PENDING again, so the Cloud Tasks
    redelivery can claim and submit it."""
    from datensee import api as datensee_api

    attempts: list[int] = []

    def flaky_export(**kwargs: Any) -> ExportResult:
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("connection refused")
        return fake_export_result

    monkeypatch.setattr(datensee_api, "export", flaky_export)

    first = client.post("/submit-task", json=_task_body())
    second = client.post("/submit-task", json=_task_body())

    assert first.status_code == 503
    assert second.status_code == 200 and second.json()["ok"] is True
    assert fake_db.rows[(USER_ID, TASK_ID)]["State"] == "SUBMITTED"
    assert len(attempts) == 2


def test_submit_task_validation_error_writes_failed_and_returns_200(
    client: TestClient,
    fake_db: FakeSpannerDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_export(monkeypatch, ValueError("bad geometry"))

    resp = client.post("/submit-task", json=_task_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "validation"

    row = fake_db.rows[(USER_ID, TASK_ID)]
    assert row["State"] == "FAILED"
    assert row["ErrorKind"] == "validation"


def test_submit_task_permanent_flex_error_is_failed_not_retried(
    client: TestClient,
    fake_db: FakeSpannerDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Flex Template 4xx (non-429) is a permanent error: FAILED + 200."""
    _patch_export(
        monkeypatch,
        RuntimeError("Flex Template launch failed (HTTP 400): bad parameter"),
    )

    resp = client.post("/submit-task", json=_task_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "runtime"
    assert fake_db.rows[(USER_ID, TASK_ID)]["State"] == "FAILED"


def test_submit_task_unknown_error_is_terminal_runtime(
    client: TestClient,
    fake_db: FakeSpannerDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrecognized exceptions must never 5xx (no infinite Cloud Tasks retry)."""
    _patch_export(monkeypatch, KeyError("some unexpected internal failure"))

    resp = client.post("/submit-task", json=_task_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "runtime"
    assert fake_db.rows[(USER_ID, TASK_ID)]["State"] == "FAILED"


def test_submit_task_malformed_body_returns_200_and_never_echoes_values(
    client: TestClient,
) -> None:
    """Cloud Tasks retries any non-2xx forever, and FastAPI's default 422
    detail echoes input values — including a mistyped access_token."""
    resp = client.post(
        "/submit-task",
        json={
            "task_id": TASK_ID,
            "user_id": USER_ID,
            "access_token": {"v": SECRET_TOKEN},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "validation"
    assert SECRET_TOKEN not in resp.text
    locs = {"/".join(entry["loc"]) for entry in body["detail"]}
    assert any("access_token" in loc for loc in locs)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_reports_datensee_version(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "datensee_version" in body


def test_health_returns_503_when_datensee_unimportable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `None in sys.modules` makes `import datensee` raise ImportError.
    monkeypatch.setitem(sys.modules, "datensee", None)

    resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "import"
