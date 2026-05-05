"""Tests for submit.py command builders and Flex Template payload."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

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
from datensee.submit import (
    _build_flex_payload,
    _build_local_command,
    _prepare_user_token_fd,
)


def _config(runner: RunnerConfig) -> PipelineConfig:
    return PipelineConfig(
        ee_expression='{"result":"0","values":{}}',
        gee_project="my-gcp-project",
        tile_grid=TileGrid(
            pixel_grid=PixelGrid(
                crs_code="EPSG:4326",
                affine_transform=AffineTransform(
                    scale_x=1.0,
                    shear_x=0.0,
                    translate_x=0.0,
                    shear_y=0.0,
                    scale_y=-1.0,
                    translate_y=1.0,
                ),
                dimensions=GridDimensions(width=1, height=1),
            ),
            tiles=[
                TileCoordinate(
                    col_px=0,
                    row_px=0,
                    width_px=1,
                    height_px=1,
                    row=0,
                    col=0,
                )
            ],
        ),
        output=OutputConfig(output_path="gs://my-bucket/exports/test"),
        runner=runner,
    )


def test_local_runner_command_has_direct_runner() -> None:
    cmd = _build_local_command(Path("/tmp/fake.jar"), Path("/tmp/cfg.json"))
    assert "--runner=DirectRunner" in cmd
    assert "--configFile=/tmp/cfg.json" in cmd


def test_flex_payload_minimal() -> None:
    df = DataflowRunnerConfig(
        project="p",
        region="us-central1",
        temp_location="gs://b/tmp",
        staging_location="gs://b/staging",
    )
    payload = _build_flex_payload(
        job_name="datensee-1234",
        spec_uri="gs://datensee-templates/v0.1.0a1/datensee.json",
        config_uri="gs://b/exports/_pipeline-config.json",
        df=df,
    )
    lp = payload["launchParameter"]
    assert lp["jobName"] == "datensee-1234"
    assert lp["containerSpecGcsPath"] == "gs://datensee-templates/v0.1.0a1/datensee.json"
    assert lp["parameters"] == {"configFile": "gs://b/exports/_pipeline-config.json"}
    assert lp["environment"]["tempLocation"] == "gs://b/tmp"
    assert lp["environment"]["stagingLocation"] == "gs://b/staging"
    assert lp["environment"]["maxWorkers"] == 100
    # Optional fields stay out of the env map when unset.
    assert "additionalUserLabels" not in lp["environment"]
    assert "serviceAccountEmail" not in lp["environment"]


def test_flex_payload_includes_labels_as_additional_user_labels() -> None:
    df = DataflowRunnerConfig(
        project="p",
        region="us-central1",
        temp_location="gs://b/tmp",
        staging_location="gs://b/staging",
        labels={"team": "geo", "stage": "alpha"},
    )
    payload = _build_flex_payload(
        job_name="datensee-1",
        spec_uri="gs://x/y.json",
        config_uri="gs://b/cfg.json",
        df=df,
    )
    assert payload["launchParameter"]["environment"]["additionalUserLabels"] == {
        "team": "geo",
        "stage": "alpha",
    }


def test_flex_payload_includes_service_account_when_set() -> None:
    df = DataflowRunnerConfig(
        project="p",
        region="us-central1",
        temp_location="gs://b/tmp",
        staging_location="gs://b/staging",
        service_account_email="worker@p.iam.gserviceaccount.com",
        network="my-vpc",
        subnetwork="regions/us-central1/subnetworks/my-sub",
    )
    payload = _build_flex_payload(
        job_name="datensee-2",
        spec_uri="gs://x/y.json",
        config_uri="gs://b/cfg.json",
        df=df,
    )
    env = payload["launchParameter"]["environment"]
    assert env["serviceAccountEmail"] == "worker@p.iam.gserviceaccount.com"
    assert env["network"] == "my-vpc"
    assert env["subnetwork"] == "regions/us-central1/subnetworks/my-sub"


def test_prepare_user_token_fd_returns_none_without_credentials() -> None:
    assert _prepare_user_token_fd(None) is None


def test_prepare_user_token_fd_round_trip() -> None:
    """Pipe FD carries exactly the token bytes and then EOFs.

    Mirrors what the JVM does on the other side: read from the inherited
    FD via /proc/self/fd/<N> (here we just read the FD directly since
    we're the same process) and confirm we see the token followed by EOF.
    Also verifies the FD is inheritable so ``subprocess.Popen(pass_fds=…)``
    can hand it to the child.
    """
    creds = MagicMock()
    creds.token = "ya29.test-token-value"
    creds.expired = False

    fd = _prepare_user_token_fd(creds)
    assert fd is not None
    try:
        assert os.get_inheritable(fd)
        data = os.read(fd, 4096)
        assert data == b"ya29.test-token-value"
        assert os.read(fd, 4096) == b""
    finally:
        os.close(fd)


def test_prepare_user_token_fd_refreshes_expired() -> None:
    creds = MagicMock()
    creds.token = None
    creds.expired = True

    def _do_refresh(_request: object) -> None:
        creds.token = "ya29.refreshed"
        creds.expired = False

    creds.refresh.side_effect = _do_refresh

    fd = _prepare_user_token_fd(creds)
    assert fd is not None
    try:
        assert os.read(fd, 4096) == b"ya29.refreshed"
    finally:
        os.close(fd)
    creds.refresh.assert_called_once()


def test_submit_dataflow_dispatches_via_flex(monkeypatch) -> None:
    """``submit_job(mode='dataflow')`` calls the Flex Template launcher."""
    from datensee import submit as submit_mod

    captured: dict[str, object] = {}

    def fake_upload(uri: str, data: bytes, **kwargs: object) -> None:
        captured["upload_uri"] = uri
        captured["upload_bytes"] = data

    def fake_launch(**kwargs: object) -> str:
        captured["launch_kwargs"] = kwargs
        return "2026-04-30_test_job"

    monkeypatch.setattr(submit_mod, "_upload_to_gcs", fake_upload)
    monkeypatch.setattr(submit_mod, "_launch_flex_template", fake_launch)

    cfg = _config(
        RunnerConfig(
            mode="dataflow",
            dataflow=DataflowRunnerConfig(
                project="p",
                region="us-central1",
                temp_location="gs://b/tmp",
                staging_location="gs://b/staging",
            ),
        )
    )
    job_id = submit_mod.submit_job(
        cfg,
        jar_path=None,
        template_spec="gs://override/spec.json",
    )
    assert job_id == "2026-04-30_test_job"
    assert captured["upload_uri"] == "gs://my-bucket/exports/test/_pipeline-config.json"
    assert captured["launch_kwargs"]["project"] == "p"
    payload = captured["launch_kwargs"]["payload"]
    assert payload["launchParameter"]["containerSpecGcsPath"] == "gs://override/spec.json"
    assert (
        payload["launchParameter"]["parameters"]["configFile"]
        == "gs://my-bucket/exports/test/_pipeline-config.json"
    )
