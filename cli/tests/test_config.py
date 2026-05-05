"""Tests for Pydantic config models."""

import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from datensee.config import (
    AffineTransform,
    CogParameters,
    DataflowRunnerConfig,
    GridDimensions,
    OutputConfig,
    PipelineConfig,
    PixelGrid,
    RateLimitConfig,
    RunnerConfig,
    TileCoordinate,
    TileGrid,
)


def _pixel_grid(*, scale: float = 30.0, width: int = 512, height: int = 512) -> PixelGrid:
    return PixelGrid(
        crs_code="EPSG:4326",
        affine_transform=AffineTransform(
            scale_x=scale,
            shear_x=0.0,
            translate_x=0.0,
            shear_y=0.0,
            scale_y=-scale,
            translate_y=0.0,
        ),
        dimensions=GridDimensions(width=width, height=height),
    )


def _tile(col_px: int = 0, row_px: int = 0, *, row: int = 0, col: int = 0) -> TileCoordinate:
    return TileCoordinate(
        col_px=col_px, row_px=row_px, width_px=512, height_px=512, row=row, col=col
    )


def _minimal_config() -> PipelineConfig:
    return PipelineConfig(
        ee_expression='{"result":"0","values":{}}',
        gee_project="my-gcp-project",
        tile_grid=TileGrid(pixel_grid=_pixel_grid(), tiles=[_tile()]),
        output=OutputConfig(output_path="gs://my-bucket/exports/test"),
    )


def test_minimal_config_is_valid() -> None:
    config = _minimal_config()
    assert config.runner.mode == "local"
    assert config.gee_project == "my-gcp-project"


def test_empty_tile_grid_raises() -> None:
    with pytest.raises(ValidationError, match="either inline tiles or a tiles_file"):
        TileGrid(pixel_grid=_pixel_grid(), tiles=[])


def test_no_tile_source_raises() -> None:
    with pytest.raises(ValidationError, match="either inline tiles or a tiles_file"):
        TileGrid(pixel_grid=_pixel_grid())


def test_both_tile_sources_raises() -> None:
    with pytest.raises(ValidationError, match="cannot have both"):
        TileGrid(
            pixel_grid=_pixel_grid(),
            tiles=[_tile()],
            tiles_file="gs://bucket/tiles.ndjson",
        )


def test_tiles_file_config() -> None:
    grid = TileGrid(
        pixel_grid=_pixel_grid(),
        tiles_file="gs://bucket/tiles.ndjson",
    )
    assert grid.tiles_file == "gs://bucket/tiles.ndjson"
    assert grid.tiles is None


def test_tile_grid_crs_property_delegates_to_pixel_grid() -> None:
    grid = TileGrid(pixel_grid=_pixel_grid(), tiles=[_tile()])
    assert grid.crs == "EPSG:4326"


def test_tile_grid_pixel_size_property_delegates_to_affine() -> None:
    grid = TileGrid(pixel_grid=_pixel_grid(scale=42.0), tiles=[_tile()])
    assert grid.pixel_size == 42.0


def test_dataflow_mode_requires_dataflow_config() -> None:
    with pytest.raises(ValidationError, match="dataflow config is required"):
        RunnerConfig(mode="dataflow", dataflow=None)


def test_cog_defaults_are_sensible() -> None:
    cog = CogParameters()
    assert cog.blocksize == 512
    assert cog.compress == "deflate"
    assert 2 in cog.overview_levels


def test_tile_grid_default_tile_size() -> None:
    grid = TileGrid(pixel_grid=_pixel_grid(), tiles=[_tile()])
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
    assert restored.tile_grid.crs == config.tile_grid.crs
    assert restored.tile_grid.pixel_size == config.tile_grid.pixel_size

    path.unlink()


def test_roundtrip_excludes_none_fields() -> None:
    config = _minimal_config()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)

    config.write_json(path)
    raw = path.read_text()

    assert "tiles_file" not in raw
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
    assert runner.dataflow.labels is None


def test_dataflow_runner_config_with_labels() -> None:
    runner = RunnerConfig(
        mode="dataflow",
        dataflow=DataflowRunnerConfig(
            project="my-project",
            region="us-central1",
            temp_location="gs://my-bucket/tmp",
            staging_location="gs://my-bucket/staging",
            labels={"foundree": "1", "team": "geo"},
        ),
    )
    assert runner.dataflow is not None
    assert runner.dataflow.labels == {"foundree": "1", "team": "geo"}


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
            tile_grid=TileGrid(pixel_grid=_pixel_grid(), tiles=[_tile()]),
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
        update={"output": OutputConfig(output_path="/tmp/out", band_count=3, data_type="uint8")}
    )
    assert config.output.band_count == 3
    assert config.output.data_type == "uint8"


def test_invalid_band_count_raises() -> None:
    with pytest.raises(ValidationError):
        OutputConfig(output_path="/tmp/out", band_count=0)


def test_invalid_data_type_raises() -> None:
    with pytest.raises(ValidationError):
        OutputConfig(output_path="/tmp/out", data_type="complex128")


def test_rate_limit_defaults() -> None:
    config = _minimal_config()
    assert config.rate_limit.max_qps == 100


def test_rate_limit_custom() -> None:
    config = _minimal_config().model_copy(update={"rate_limit": RateLimitConfig(max_qps=50)})
    assert config.rate_limit.max_qps == 50


def test_rate_limit_invalid_zero() -> None:
    with pytest.raises(ValidationError):
        RateLimitConfig(max_qps=0)


# ---------------------------------------------------------------------------
# expected_output_tile_count: M6 two-tier accounting
# ---------------------------------------------------------------------------


def _config_with_tiles(
    tiles: list[TileCoordinate], output_tile_size: int | None = None
) -> PipelineConfig:
    return PipelineConfig(
        ee_expression='{"result":"0","values":{}}',
        gee_project="my-gcp-project",
        tile_grid=TileGrid(
            pixel_grid=_pixel_grid(scale=30.0, width=4096, height=4096),
            tile_size_pixels=64,
            tiles=tiles,
        ),
        output=OutputConfig(output_path="gs://b/p", output_tile_size_pixels=output_tile_size),
    )


def test_expected_output_tile_count_defaults_to_compute_tile_count() -> None:
    tiles = [
        TileCoordinate(
            col_px=i * 64,
            row_px=0,
            width_px=64,
            height_px=64,
            row=0,
            col=i,
        )
        for i in range(5)
    ]
    config = _config_with_tiles(tiles)
    assert config.expected_output_tile_count == 5
    assert config.expected_output_tile_count == config.tile_count


def test_expected_output_tile_count_groups_by_out_row_out_col() -> None:
    # 4 compute tiles, all sharing (out_row=0, out_col=0): one output tile.
    tiles = [
        TileCoordinate(
            col_px=i * 64,
            row_px=0,
            width_px=64,
            height_px=64,
            row=0,
            col=i,
            out_row=0,
            out_col=0,
        )
        for i in range(4)
    ]
    config = _config_with_tiles(tiles, output_tile_size=128)
    assert config.tile_count == 4
    assert config.expected_output_tile_count == 1


def test_expected_output_tile_count_distinct_groups() -> None:
    tiles = []
    for out_col in range(2):
        for i in range(4):
            tiles.append(
                TileCoordinate(
                    col_px=(out_col * 4 + i) * 64,
                    row_px=0,
                    width_px=64,
                    height_px=64,
                    row=0,
                    col=out_col * 4 + i,
                    out_row=0,
                    out_col=out_col,
                )
            )
    config = _config_with_tiles(tiles, output_tile_size=128)
    assert config.tile_count == 8
    assert config.expected_output_tile_count == 2


def test_expected_output_tile_count_external_tiles_file() -> None:
    config = PipelineConfig(
        ee_expression='{"result":"0","values":{}}',
        gee_project="p",
        tile_grid=TileGrid(
            pixel_grid=_pixel_grid(),
            tiles_file="gs://b/tiles.ndjson",
        ),
        output=OutputConfig(output_path="gs://b/p"),
    )
    assert config.expected_output_tile_count == 0
