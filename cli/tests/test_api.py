"""Tests for datensee.api — public Python API surface."""

from __future__ import annotations

import pytest

from datensee.api import (
    _DEMO_EXPRESSION,
    _DEMO_REGION,
    ExportResult,
    tile,
)


class TestTile:
    def test_returns_tile_grid(self) -> None:
        grid = tile(_DEMO_REGION, scale=30.0, crs="EPSG:4326", tile_size=512)
        assert grid.tiles is not None
        assert len(grid.tiles) > 0
        assert grid.crs == "EPSG:4326"
        assert grid.scale_meters == 30.0

    def test_tile_count_varies_with_scale(self) -> None:
        grid_30 = tile(_DEMO_REGION, scale=30.0)
        grid_100 = tile(_DEMO_REGION, scale=100.0)
        assert len(grid_30.tiles) >= len(grid_100.tiles)


class TestExportResult:
    def test_model_fields(self) -> None:
        from datensee.config import (
            OutputConfig,
            PipelineConfig,
            RunnerConfig,
        )
        from datensee.estimate import CostEstimate

        grid = tile(_DEMO_REGION, scale=30.0)
        config = PipelineConfig(
            ee_expression=_DEMO_EXPRESSION,
            gee_project="test-project",
            tile_grid=grid,
            output=OutputConfig(output_path="/tmp/test"),
            runner=RunnerConfig(mode="local"),
        )
        estimate = CostEstimate(
            tile_count=4,
            estimated_wall_seconds=1.0,
            eecu_seconds_low=4.0,
            eecu_seconds_typical=12.0,
            eecu_seconds_high=40.0,
            output_size_bytes=1024,
            storage_cost_usd_per_month=0.0001,
        )
        result = ExportResult(config=config, estimate=estimate)
        assert result.job_id is None
        assert result.duration_seconds is None
        assert result.vrt_path is None


class TestExportValidation:
    def test_export_raises_on_bad_inputs(self) -> None:
        from datensee.api import export

        with pytest.raises(ValueError, match="not valid JSON"):
            export(
                ee_expression="not json",
                region=_DEMO_REGION,
                project="test",
                output="/tmp/out",
                runner="local",
                dry_run=True,
            )

    def test_export_raises_on_missing_temp_location(self) -> None:
        from datensee.api import export

        with pytest.raises(ValueError, match="temp-location"):
            export(
                ee_expression=_DEMO_EXPRESSION,
                region=_DEMO_REGION,
                project="test",
                output="gs://bucket/out",
                runner="dataflow",
                dry_run=True,
            )

    def test_export_dry_run_succeeds(self) -> None:
        from datensee.api import export

        result = export(
            ee_expression=_DEMO_EXPRESSION,
            region=_DEMO_REGION,
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
