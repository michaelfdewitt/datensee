"""Tests for datensee.display: Rich rendering module.

Smoke tests: verify that rendering functions return the expected types
and don't crash on edge cases.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

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
from datensee.display import render_export_summary, render_post_run_summary

_PIXEL_SIZE = 30.0 / 111_320.0
_TILE_SIZE = 512


def _pixel_grid(n_tiles_wide: int, n_tiles_tall: int = 1) -> PixelGrid:
    return PixelGrid(
        crs_code="EPSG:4326",
        affine_transform=AffineTransform(
            scale_x=_PIXEL_SIZE,
            shear_x=0.0,
            translate_x=0.0,
            shear_y=0.0,
            scale_y=-_PIXEL_SIZE,
            translate_y=n_tiles_tall * _TILE_SIZE * _PIXEL_SIZE,
        ),
        dimensions=GridDimensions(
            width=n_tiles_wide * _TILE_SIZE, height=n_tiles_tall * _TILE_SIZE
        ),
    )


def _make_tiles(n: int) -> list[TileCoordinate]:
    return [
        TileCoordinate(
            col_px=i * _TILE_SIZE,
            row_px=0,
            width_px=_TILE_SIZE,
            height_px=_TILE_SIZE,
            row=0,
            col=i,
        )
        for i in range(n)
    ]


def _make_local_config() -> PipelineConfig:
    return PipelineConfig(
        ee_expression='{"result":"0","values":{"0":{"constantValue":1}}}',
        gee_project="test",
        tile_grid=TileGrid(pixel_grid=_pixel_grid(100), tiles=_make_tiles(100)),
        output=OutputConfig(output_path="/tmp/output"),
        runner=RunnerConfig(mode="local"),
    )


def _render_text(panel: Panel) -> str:
    console = Console(width=200)
    with console.capture() as capture:
        console.print(panel)
    return capture.get()


def _make_dataflow_config() -> PipelineConfig:
    return PipelineConfig(
        ee_expression='{"result":"0","values":{"0":{"constantValue":1}}}',
        gee_project="test",
        tile_grid=TileGrid(pixel_grid=_pixel_grid(1000), tiles=_make_tiles(1000)),
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


class TestExportSummaryCostSection:
    def test_local_config_shows_cost_section(self) -> None:
        text = _render_text(render_export_summary(_make_local_config()))
        assert "Est. EECU" in text
        assert "free for non-commercial EE use" in text
        assert "none (local runner)" in text
        assert "Est. storage" in text
        assert "rough estimate" in text

    def test_dataflow_config_shows_usd_range(self) -> None:
        text = _render_text(render_export_summary(_make_dataflow_config()))
        assert "Est. Dataflow" in text
        assert "$" in text
        assert "Est. shuffle" not in text  # not two-tier

    def test_m6_dataflow_config_shows_shuffle_line(self) -> None:
        config = _make_dataflow_config()
        config = config.model_copy(deep=True)
        config.pixel.output.output_tile_size_pixels = _TILE_SIZE * 4
        text = _render_text(render_export_summary(config))
        assert "Est. shuffle" in text

    def test_externalized_tiles_shows_single_unavailable_line(self) -> None:
        config = PipelineConfig(
            ee_expression='{"result":"0","values":{"0":{"constantValue":1}}}',
            gee_project="test",
            tile_grid=TileGrid(pixel_grid=_pixel_grid(100), tiles_file="gs://bucket/tiles.ndjson"),
            output=OutputConfig(output_path="gs://bucket/output"),
            runner=RunnerConfig(mode="local"),
        )
        text = _render_text(render_export_summary(config))
        assert "unavailable" in text
        assert "Est. EECU" not in text


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
