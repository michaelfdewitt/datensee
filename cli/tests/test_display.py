"""Tests for datensee.display — Rich rendering module.

Smoke tests: verify that rendering functions return the expected types
and don't crash on edge cases.
"""

from __future__ import annotations

from rich.panel import Panel

from datensee.config import (
    DataflowRunnerConfig,
    OutputConfig,
    PipelineConfig,
    RateLimitConfig,
    RunnerConfig,
    TileCoordinate,
    TileGrid,
)
from datensee.display import render_export_summary, render_post_run_summary


def _make_tiles(n: int) -> list[TileCoordinate]:
    return [TileCoordinate(x_min=i, y_min=0, x_max=i + 1, y_max=1, row=0, col=i) for i in range(n)]


def _make_local_config() -> PipelineConfig:
    return PipelineConfig(
        ee_expression='{"result":"0","values":{"0":{"constantValue":1}}}',
        gee_project="test",
        tile_grid=TileGrid(crs="EPSG:4326", scale_meters=30.0, tiles=_make_tiles(100)),
        output=OutputConfig(output_path="/tmp/output"),
        runner=RunnerConfig(mode="local"),
    )


def _make_dataflow_config() -> PipelineConfig:
    return PipelineConfig(
        ee_expression='{"result":"0","values":{"0":{"constantValue":1}}}',
        gee_project="test",
        tile_grid=TileGrid(crs="EPSG:4326", scale_meters=30.0, tiles=_make_tiles(1000)),
        output=OutputConfig(output_path="gs://bucket/output"),
        runner=RunnerConfig(
            mode="dataflow",
            dataflow=DataflowRunnerConfig(
                project="test",
                region="us-central1",
                temp_location="gs://tmp/temp",
                staging_location="gs://tmp/staging",
            ),
        ),
        rate_limit=RateLimitConfig(max_qps=200),
    )


class TestRenderExportSummary:
    def test_returns_panel_local(self) -> None:
        panel = render_export_summary(_make_local_config())
        assert isinstance(panel, Panel)

    def test_returns_panel_dataflow(self) -> None:
        panel = render_export_summary(_make_dataflow_config())
        assert isinstance(panel, Panel)

    def test_zero_tiles(self) -> None:
        config = _make_local_config()
        panel = render_export_summary(config)
        assert isinstance(panel, Panel)


class TestRenderPostRunSummary:
    def test_success(self) -> None:
        panel = render_post_run_summary(12.5, 100, 0, "/tmp/output")
        assert isinstance(panel, Panel)

    def test_with_failures(self) -> None:
        panel = render_post_run_summary(30.0, 95, 5, "gs://bucket/output")
        assert isinstance(panel, Panel)

    def test_zero_tiles(self) -> None:
        panel = render_post_run_summary(0.1, 0, 0, "/tmp/empty")
        assert isinstance(panel, Panel)

    def test_with_output_bytes(self) -> None:
        panel = render_post_run_summary(12.5, 100, 0, "/tmp/out", output_bytes=1024 * 1024)
        assert isinstance(panel, Panel)
