"""Tests for Pydantic config models."""

import json
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from gee_df.config import (
    CogParameters,
    DataflowRunnerConfig,
    OutputConfig,
    PipelineConfig,
    RunnerConfig,
    TileCoordinate,
    TileGrid,
)


def _minimal_config() -> PipelineConfig:
    return PipelineConfig(
        ee_expression='{"type":"Image","bandNames":["NDVI"]}',
        tile_grid=TileGrid(
            crs="EPSG:4326",
            scale_meters=30.0,
            tiles=[TileCoordinate(x_min=0, y_min=0, x_max=1, y_max=1, row=0, col=0)],
        ),
        output=OutputConfig(gcs_path="gs://my-bucket/exports/test"),
    )


def test_minimal_config_is_valid() -> None:
    config = _minimal_config()
    assert config.runner.mode == "local"


def test_empty_tile_grid_raises() -> None:
    with pytest.raises(ValidationError, match="at least one tile"):
        TileGrid(crs="EPSG:4326", scale_meters=30.0, tiles=[])


def test_dataflow_mode_requires_dataflow_config() -> None:
    with pytest.raises(ValidationError, match="dataflow config is required"):
        RunnerConfig(mode="dataflow", dataflow=None)


def test_cog_defaults_are_sensible() -> None:
    cog = CogParameters()
    assert cog.blocksize == 512
    assert cog.compress == "lzw"
    assert 2 in cog.overview_levels


def test_roundtrip_json_serialization() -> None:
    config = _minimal_config()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)

    config.write_json(path)
    restored = PipelineConfig.read_json(path)

    assert restored.ee_expression == config.ee_expression
    assert restored.runner.mode == config.runner.mode
    assert len(restored.tile_grid.tiles) == len(config.tile_grid.tiles)

    path.unlink()


def test_dataflow_runner_config() -> None:
    runner = RunnerConfig(
        mode="dataflow",
        dataflow=DataflowRunnerConfig(
            project="my-project",
            region="us-central1",
            temp_location="gs://my-bucket/tmp",
            staging_location="gs://my-bucket/staging",
        ),
    )
    assert runner.dataflow is not None
    assert runner.dataflow.max_workers == 100
