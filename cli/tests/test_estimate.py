"""Tests for datensee.estimate — cost estimation module."""

from __future__ import annotations

import pytest

from datensee.config import (
    DataflowRunnerConfig,
    OutputConfig,
    PipelineConfig,
    RateLimitConfig,
    RunnerConfig,
    TileCoordinate,
    TileGrid,
)
from datensee.estimate import (
    CostEstimate,
    _bytes_per_pixel,
    _dataflow_regional_rates,
    _machine_specs,
    estimate_cost,
)


def _make_tiles(n: int) -> list[TileCoordinate]:
    """Generate n dummy tile coordinates."""
    return [
        TileCoordinate(x_min=i, y_min=0, x_max=i + 1, y_max=1, row=0, col=i)
        for i in range(n)
    ]


def _make_config(
    *,
    tile_count: int = 100,
    runner_mode: str = "local",
    machine_type: str = "n2-standard-4",
    max_workers: int = 10,
    max_qps: int = 100,
    data_type: str = "float32",
    band_count: int = 1,
    tile_size_pixels: int = 512,
    region: str = "us-central1",
) -> PipelineConfig:
    """Build a minimal PipelineConfig for testing."""
    dataflow = None
    if runner_mode == "dataflow":
        dataflow = DataflowRunnerConfig(
            project="test-project",
            region=region,
            temp_location="gs://tmp/temp",
            staging_location="gs://tmp/staging",
            machine_type=machine_type,
            max_workers=max_workers,
        )

    return PipelineConfig(
        ee_expression='{"result":"0","values":{"0":{"constantValue":1}}}',
        gee_project="test-project",
        tile_grid=TileGrid(
            crs="EPSG:4326",
            scale_meters=30.0,
            tile_size_pixels=tile_size_pixels,
            tiles=_make_tiles(tile_count),
        ),
        output=OutputConfig(
            output_path="gs://bucket/output" if runner_mode == "dataflow" else "/tmp/output",
            data_type=data_type,
            band_count=band_count,
        ),
        runner=RunnerConfig(mode=runner_mode, dataflow=dataflow),
        rate_limit=RateLimitConfig(max_qps=max_qps),
    )


class TestMachineSpecs:
    def test_known_machine(self) -> None:
        vcpus, mem = _machine_specs("n2-standard-4")
        assert vcpus == 4
        assert mem == 16.0

    def test_unknown_pattern_fallback(self) -> None:
        vcpus, mem = _machine_specs("n2-standard-128")
        assert vcpus == 128
        assert mem == 512.0

    def test_unrecognized_machine(self) -> None:
        vcpus, mem = _machine_specs("custom-weird-thing")
        assert vcpus == 4
        assert mem == 16.0


class TestBytesPerPixel:
    def test_float32(self) -> None:
        assert _bytes_per_pixel("float32") == 4

    def test_uint8(self) -> None:
        assert _bytes_per_pixel("uint8") == 1

    def test_unknown_defaults_to_4(self) -> None:
        assert _bytes_per_pixel("bfloat16") == 4


class TestDataflowRegionalRates:
    def test_known_region(self) -> None:
        vcpu, gb = _dataflow_regional_rates("us-central1")
        assert vcpu == 0.056
        assert gb == 0.003557

    def test_unknown_region_uses_default(self) -> None:
        vcpu, gb = _dataflow_regional_rates("mars-west1")
        assert vcpu == 0.056


class TestEstimateCostLocal:
    def test_basic_local_estimate(self) -> None:
        config = _make_config(tile_count=100, max_qps=100)
        est = estimate_cost(config)

        assert est.tile_count == 100
        assert est.estimated_wall_seconds == pytest.approx(100 / (100 * 0.7))
        assert est.eecu_seconds_low == pytest.approx(100.0)
        assert est.eecu_seconds_typical == pytest.approx(300.0)
        assert est.eecu_seconds_high == pytest.approx(1000.0)
        assert est.dataflow_cost_usd is None
        assert est.dataflow_vcpu_hours is None
        assert est.dataflow_memory_gb_hours is None
        assert est.output_size_bytes > 0
        assert est.storage_cost_usd_per_month > 0

    def test_custom_eecu_per_tile(self) -> None:
        config = _make_config(tile_count=50)
        est = estimate_cost(config, eecu_per_tile=2.0)

        assert est.eecu_seconds_low == pytest.approx(100.0)
        assert est.eecu_seconds_typical == pytest.approx(300.0)
        assert est.eecu_seconds_high == pytest.approx(1000.0)

    def test_output_size_scales_with_bands(self) -> None:
        est1 = estimate_cost(_make_config(band_count=1))
        est3 = estimate_cost(_make_config(band_count=3))
        assert est3.output_size_bytes == pytest.approx(est1.output_size_bytes * 3)

    def test_output_size_scales_with_data_type(self) -> None:
        est_f32 = estimate_cost(_make_config(data_type="float32"))
        est_u8 = estimate_cost(_make_config(data_type="uint8"))
        assert est_f32.output_size_bytes == pytest.approx(est_u8.output_size_bytes * 4)


class TestEstimateCostDataflow:
    def test_dataflow_costs_populated(self) -> None:
        config = _make_config(
            tile_count=1000,
            runner_mode="dataflow",
            max_qps=100,
            max_workers=10,
        )
        est = estimate_cost(config)

        assert est.dataflow_cost_usd is not None
        assert est.dataflow_cost_usd > 0
        assert est.dataflow_vcpu_hours is not None
        assert est.dataflow_vcpu_hours > 0
        assert est.dataflow_memory_gb_hours is not None

    def test_more_workers_costs_more(self) -> None:
        est_10 = estimate_cost(
            _make_config(tile_count=1000, runner_mode="dataflow", max_workers=10)
        )
        est_100 = estimate_cost(
            _make_config(tile_count=1000, runner_mode="dataflow", max_workers=100)
        )
        assert est_100.dataflow_cost_usd > est_10.dataflow_cost_usd


class TestEstimateZeroTiles:
    def test_zero_tiles_no_crash(self) -> None:
        """Config with tiles_file instead of inline tiles → tile_count=0."""
        config = PipelineConfig(
            ee_expression='{"result":"0","values":{"0":{"constantValue":1}}}',
            gee_project="test",
            tile_grid=TileGrid(
                crs="EPSG:4326",
                scale_meters=30.0,
                tiles_file="gs://bucket/_tiles.ndjson",
            ),
            output=OutputConfig(output_path="gs://bucket/output"),
            runner=RunnerConfig(mode="local"),
        )
        est = estimate_cost(config)
        assert est.tile_count == 0
        assert est.output_size_bytes == 0
