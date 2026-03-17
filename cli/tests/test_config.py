"""Tests for Pydantic config models."""

import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from datensee.config import (
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
        ee_expression='{"result":"0","values":{}}',
        gee_project="my-gcp-project",
        tile_grid=TileGrid(
            crs="EPSG:4326",
            scale_meters=30.0,
            tiles=[TileCoordinate(x_min=0, y_min=0, x_max=1, y_max=1, row=0, col=0)],
        ),
        output=OutputConfig(output_path="gs://my-bucket/exports/test"),
    )


def test_minimal_config_is_valid() -> None:
    config = _minimal_config()
    assert config.runner.mode == "local"
    assert config.gee_project == "my-gcp-project"


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


def test_tile_grid_default_tile_size() -> None:
    grid = TileGrid(
        crs="EPSG:4326",
        scale_meters=30.0,
        tiles=[TileCoordinate(x_min=0, y_min=0, x_max=1, y_max=1, row=0, col=0)],
    )
    assert grid.tile_size_pixels == 512


def test_roundtrip_json_serialization() -> None:
    config = _minimal_config()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)

    config.write_json(path)
    restored = PipelineConfig.read_json(path)

    assert restored.ee_expression == config.ee_expression
    assert restored.gee_project == config.gee_project
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


def test_local_output_path_accepted() -> None:
    config = _minimal_config().model_copy(
        update={"output": OutputConfig(output_path="/tmp/datensee-output")}
    )
    assert config.output.output_path == "/tmp/datensee-output"


def test_invalid_ee_expression_raises() -> None:
    with pytest.raises(ValidationError, match="valid JSON"):
        PipelineConfig(
            ee_expression="not valid json {{{",
            gee_project="my-project",
            tile_grid=TileGrid(
                crs="EPSG:4326",
                scale_meters=30.0,
                tiles=[
                    TileCoordinate(x_min=0, y_min=0, x_max=1, y_max=1, row=0, col=0)
                ],
            ),
            output=OutputConfig(output_path="/tmp/out"),
        )


def test_valid_ee_expression_accepted() -> None:
    config = _minimal_config()
    assert config.ee_expression == '{"result":"0","values":{}}'


def test_band_count_default() -> None:
    config = _minimal_config()
    assert config.output.band_count == 1


def test_data_type_default() -> None:
    config = _minimal_config()
    assert config.output.data_type == "float32"


def test_multiband_config() -> None:
    config = _minimal_config().model_copy(
        update={
            "output": OutputConfig(
                output_path="/tmp/out", band_count=3, data_type="uint8"
            )
        }
    )
    assert config.output.band_count == 3
    assert config.output.data_type == "uint8"


def test_invalid_band_count_raises() -> None:
    with pytest.raises(ValidationError):
        OutputConfig(output_path="/tmp/out", band_count=0)


def test_invalid_data_type_raises() -> None:
    with pytest.raises(ValidationError):
        OutputConfig(output_path="/tmp/out", data_type="complex128")
