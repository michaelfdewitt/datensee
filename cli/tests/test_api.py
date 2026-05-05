"""Tests for datensee.api — public Python API surface."""

from __future__ import annotations

import pytest

from datensee.api import (
    ExportResult,
    demo_expression,
    demo_region,
    tile,
)


class TestTile:
    def test_returns_tile_grid(self) -> None:
        grid = tile(demo_region(), scale=30.0, crs="EPSG:4326", tile_size=512)
        assert grid.tiles is not None
        assert len(grid.tiles) > 0
        assert grid.crs == "EPSG:4326"
        # 30 m at the equator constant ≈ 0.0002695 °/px.
        assert grid.pixel_size > 0
        assert grid.pixel_size < 0.001

    def test_tile_count_varies_with_scale(self) -> None:
        region = demo_region()
        grid_30 = tile(region, scale=30.0)
        grid_100 = tile(region, scale=100.0)
        assert len(grid_30.tiles) >= len(grid_100.tiles)


class TestExportResult:
    def test_model_fields(self) -> None:
        from datensee.config import (
            OutputConfig,
            PipelineConfig,
            RunnerConfig,
        )

        grid = tile(demo_region(), scale=30.0)
        config = PipelineConfig(
            ee_expression=demo_expression(),
            gee_project="test-project",
            tile_grid=grid,
            output=OutputConfig(output_path="/tmp/test"),
            runner=RunnerConfig(mode="local"),
        )
        result = ExportResult(config=config)
        assert result.job_id is None
        assert result.duration_seconds is None
        assert result.output_bytes is None


class TestExportValidation:
    def test_export_raises_on_bad_inputs(self) -> None:
        from datensee.api import export

        with pytest.raises(ValueError, match="not valid JSON"):
            export(
                ee_expression="not json",
                region=demo_region(),
                project="test",
                output="/tmp/out",
                runner="local",
                dry_run=True,
            )

    def test_export_raises_on_missing_temp_location(self) -> None:
        from datensee.api import export

        with pytest.raises(ValueError, match="temp-location"):
            export(
                ee_expression=demo_expression(),
                region=demo_region(),
                project="test",
                output="gs://bucket/out",
                runner="dataflow",
                dry_run=True,
            )

    def test_export_dry_run_succeeds(self) -> None:
        from datensee.api import export

        result = export(
            ee_expression=demo_expression(),
            region=demo_region(),
            project="test",
            output="gs://bucket/out",
            runner="dataflow",
            temp_location="gs://bucket/tmp",
            dry_run=True,
        )
        assert isinstance(result, ExportResult)
        assert result.job_id is None
        assert result.config.runner.mode == "dataflow"


class TestPublicImports:
    def test_top_level_imports(self) -> None:
        from datensee import ExportResult, demo, export, poll, tile

        assert callable(export)
        assert callable(demo)
        assert callable(poll)
        assert callable(tile)
        assert issubclass(ExportResult, object)
