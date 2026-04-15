"""Tests for `_build_command` — the Dataflow/Direct runner argv builder."""

from __future__ import annotations

import json
from pathlib import Path

from datensee.config import (
    DataflowRunnerConfig,
    OutputConfig,
    PipelineConfig,
    RunnerConfig,
    TileCoordinate,
    TileGrid,
)
from datensee.submit import _build_command


def _config(runner: RunnerConfig) -> PipelineConfig:
    return PipelineConfig(
        ee_expression='{"result":"0","values":{}}',
        gee_project="my-gcp-project",
        tile_grid=TileGrid(
            crs="EPSG:4326",
            scale_meters=30.0,
            tiles=[TileCoordinate(x_min=0, y_min=0, x_max=1, y_max=1, row=0, col=0)],
        ),
        output=OutputConfig(output_path="gs://my-bucket/exports/test"),
        runner=runner,
    )


def test_local_runner_command_has_direct_runner() -> None:
    cmd = _build_command(
        _config(RunnerConfig(mode="local")),
        jar_path=Path("/tmp/fake.jar"),
        config_path=Path("/tmp/cfg.json"),
    )
    assert "--runner=DirectRunner" in cmd
    assert not any(c.startswith("--labels") for c in cmd)


def test_dataflow_command_omits_labels_when_none() -> None:
    cmd = _build_command(
        _config(
            RunnerConfig(
                mode="dataflow",
                dataflow=DataflowRunnerConfig(
                    project="p",
                    region="us-central1",
                    temp_location="gs://b/tmp",
                    staging_location="gs://b/staging",
                ),
            )
        ),
        jar_path=Path("/tmp/fake.jar"),
        config_path=Path("/tmp/cfg.json"),
    )
    assert "--runner=DataflowRunner" in cmd
    assert not any(c.startswith("--labels") for c in cmd)


def test_dataflow_command_includes_labels_as_json() -> None:
    cmd = _build_command(
        _config(
            RunnerConfig(
                mode="dataflow",
                dataflow=DataflowRunnerConfig(
                    project="p",
                    region="us-central1",
                    temp_location="gs://b/tmp",
                    staging_location="gs://b/staging",
                    labels={"foundree": "1"},
                ),
            )
        ),
        jar_path=Path("/tmp/fake.jar"),
        config_path=Path("/tmp/cfg.json"),
    )
    label_flags = [c for c in cmd if c.startswith("--labels=")]
    assert len(label_flags) == 1
    payload = label_flags[0].removeprefix("--labels=")
    assert json.loads(payload) == {"foundree": "1"}


def test_dataflow_command_labels_multi_key() -> None:
    cmd = _build_command(
        _config(
            RunnerConfig(
                mode="dataflow",
                dataflow=DataflowRunnerConfig(
                    project="p",
                    region="us-central1",
                    temp_location="gs://b/tmp",
                    staging_location="gs://b/staging",
                    labels={"foundree": "1", "team": "geo"},
                ),
            )
        ),
        jar_path=Path("/tmp/fake.jar"),
        config_path=Path("/tmp/cfg.json"),
    )
    label_flags = [c for c in cmd if c.startswith("--labels=")]
    assert len(label_flags) == 1
    payload = label_flags[0].removeprefix("--labels=")
    assert json.loads(payload) == {"foundree": "1", "team": "geo"}
