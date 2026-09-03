"""Tests for submit.py command builders, Flex Template payload, and CLI parsing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import typer
from typer.testing import CliRunner

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
    assert lp["parameters"] == {
        "configFile": "gs://b/exports/_pipeline-config.json",
        "autoscalingAlgorithm": "THROUGHPUT_BASED",
        "numberOfWorkerHarnessThreads": "8",
    }
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


# ---------------------------------------------------------------------------
# --snapshot-time parsing
# ---------------------------------------------------------------------------

_NOON_UTC_MICROS = int(datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC).timestamp() * 1_000_000)


def test_parse_snapshot_time_none_passthrough() -> None:
    from datensee.main import _parse_snapshot_time

    assert _parse_snapshot_time(None) is None


def test_parse_snapshot_time_unix_micros_literal() -> None:
    from datensee.main import _parse_snapshot_time

    assert _parse_snapshot_time("1715000000000000") == 1715000000000000


def test_parse_snapshot_time_iso_z_suffix() -> None:
    from datensee.main import _parse_snapshot_time

    assert _parse_snapshot_time("2026-04-30T12:00:00Z") == _NOON_UTC_MICROS


def test_parse_snapshot_time_naive_iso_is_utc() -> None:
    """A naive ISO timestamp is UTC, as the --snapshot-time help promises.

    Regression: it used to be interpreted in the machine's local timezone.
    """
    from datensee.main import _parse_snapshot_time

    assert _parse_snapshot_time("2026-04-30T12:00:00") == _NOON_UTC_MICROS


def test_parse_snapshot_time_explicit_offset_is_honored() -> None:
    from datensee.main import _parse_snapshot_time

    assert _parse_snapshot_time("2026-04-30T14:00:00+02:00") == _NOON_UTC_MICROS


def test_parse_snapshot_time_rejects_garbage() -> None:
    from datensee.main import _parse_snapshot_time

    with pytest.raises(typer.BadParameter, match="neither Unix micros nor ISO-8601"):
        _parse_snapshot_time("not-a-time")


# ---------------------------------------------------------------------------
# validate command: the two-check surface
# ---------------------------------------------------------------------------


@pytest.fixture()
def validate_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Config file on disk + validate_output stub capturing its kwargs."""
    import datensee.pixel.validation as validation_mod

    config_file = tmp_path / "_pipeline-config.json"
    config_file.write_text(_config(RunnerConfig(mode="local")).model_dump_json())

    report = MagicMock()
    report.render.return_value = "report rendered"
    report.all_passed = True

    captured: dict[str, object] = {"called": False}

    def fake_validate_output(
        output_path: object,
        config: object,
        *,
        pixels: bool = False,
        sample: int = 20,
        gee_project: object = None,
    ) -> MagicMock:
        captured["called"] = True
        captured["pixels"] = pixels
        captured["sample"] = sample
        captured["gee_project"] = gee_project
        return report

    monkeypatch.setattr(validation_mod, "validate_output", fake_validate_output)
    return {
        "config_file": config_file,
        "captured": captured,
        "output_dir": tmp_path,
        "report": report,
    }


def _invoke_validate(setup: dict[str, object], *extra: str) -> object:
    from datensee.main import app

    return CliRunner().invoke(
        app,
        [
            "validate",
            str(setup["output_dir"]),
            "--config",
            str(setup["config_file"]),
            *extra,
        ],
    )


def test_validate_defaults_to_integrity_only(validate_setup: dict) -> None:
    result = _invoke_validate(validate_setup)
    assert result.exit_code == 0
    captured = validate_setup["captured"]  # type: ignore[assignment]
    assert captured["pixels"] is False  # type: ignore[index]


def test_validate_pixels_flag_enables_ee_comparison(validate_setup: dict) -> None:
    result = _invoke_validate(validate_setup, "--pixels", "--sample", "7")
    assert result.exit_code == 0
    captured = validate_setup["captured"]  # type: ignore[assignment]
    assert captured["pixels"] is True  # type: ignore[index]
    assert captured["sample"] == 7  # type: ignore[index]


def test_validate_failure_exits_nonzero(validate_setup: dict) -> None:
    validate_setup["report"].all_passed = False  # type: ignore[index]
    result = _invoke_validate(validate_setup)
    assert result.exit_code == 1


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ('openjdk version "25.0.4.1" 2026-08-18', 25),
        ('openjdk version "21.0.4" 2024-07-16 LTS', 21),
        ('java version "1.8.0_392"', 8),
        ('openjdk version "17" 2021-09-14', 17),
        ("The operation couldn't be completed. Unable to locate a Java Runtime.", None),
    ],
)
def test_parse_java_major(output: str, expected: int | None) -> None:
    from datensee.submit import _parse_java_major

    assert _parse_java_major(output) == expected


def test_require_java_rejects_old_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from datensee import submit as submit_mod

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr='openjdk version "11.0.2"')

    monkeypatch.setattr(submit_mod.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="Java 11 found .* needs Java 21"):
        submit_mod._require_java()
