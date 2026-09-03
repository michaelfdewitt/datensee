"""Tests for datensee.cost: pure-math cost model.

Pins the pricing model: any constant or formula change in cost.py must
show up as a deliberate edit here.
"""

from __future__ import annotations

import pytest

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
from datensee.cost import (
    DATAFLOW_SHUFFLE_USD_PER_GB,
    DEFLATE_COMPRESSION_FACTOR,
    EECU_SECONDS_PER_TILE_HIGH,
    EECU_SECONDS_PER_TILE_LOW,
    GCS_STORAGE_USD_PER_GB_MONTH,
    CostEstimate,
    estimate_cost,
    parse_machine_shape,
)

_PIXEL_SIZE = 30.0 / 111_320.0
_TILE_SIZE = 512


def _pixel_grid(n_tiles_wide: int) -> PixelGrid:
    return PixelGrid(
        crs_code="EPSG:4326",
        affine_transform=AffineTransform(
            scale_x=_PIXEL_SIZE,
            translate_x=0.0,
            scale_y=-_PIXEL_SIZE,
            translate_y=_TILE_SIZE * _PIXEL_SIZE,
        ),
        dimensions=GridDimensions(width=n_tiles_wide * _TILE_SIZE, height=_TILE_SIZE),
    )


def _make_tiles(n: int) -> list[TileCoordinate]:
    return [
        TileCoordinate(col_px=i * _TILE_SIZE, row_px=0, width_px=_TILE_SIZE, height_px=_TILE_SIZE)
        for i in range(n)
    ]


def _make_config(
    n_tiles: int,
    *,
    mode: str = "local",
    output_tile_size_pixels: int | None = None,
    machine_type: str = "n2-standard-4",
    max_workers: int = 100,
    tiles_file: str | None = None,
) -> PipelineConfig:
    tile_grid = (
        TileGrid(pixel_grid=_pixel_grid(max(n_tiles, 1)), tiles_file=tiles_file)
        if tiles_file is not None
        else TileGrid(pixel_grid=_pixel_grid(n_tiles), tiles=_make_tiles(n_tiles))
    )
    runner = (
        RunnerConfig(
            mode="dataflow",
            dataflow=DataflowRunnerConfig(
                project="test",
                region="us-central1",
                temp_location="gs://tmp/temp",
                staging_location="gs://tmp/staging",
                machine_type=machine_type,
                max_workers=max_workers,
            ),
        )
        if mode == "dataflow"
        else RunnerConfig(mode="local")
    )
    return PipelineConfig(
        ee_expression='{"result":"0","values":{"0":{"constantValue":1}}}',
        gee_project="test",
        tile_grid=tile_grid,
        output=OutputConfig(
            output_path="gs://bucket/out" if mode == "dataflow" else "/tmp/out",
            output_tile_size_pixels=output_tile_size_pixels,
        ),
        runner=runner,
    )


class TestEecuModel:
    def test_range_scales_with_tile_count(self) -> None:
        estimate = estimate_cost(_make_config(1000))
        assert estimate.eecu_seconds_low == pytest.approx(1000 * EECU_SECONDS_PER_TILE_LOW)
        assert estimate.eecu_seconds_high == pytest.approx(1000 * EECU_SECONDS_PER_TILE_HIGH)

    def test_eecu_hours_properties(self) -> None:
        estimate = estimate_cost(_make_config(1000))
        assert estimate.eecu_hours_low == pytest.approx(estimate.eecu_seconds_low / 3600.0)
        assert estimate.eecu_hours_high == pytest.approx(estimate.eecu_seconds_high / 3600.0)


class TestDataflowModel:
    def test_local_mode_has_no_dataflow_cost(self) -> None:
        estimate = estimate_cost(_make_config(1000, mode="local"))
        assert estimate.dataflow_usd_low is None
        assert estimate.dataflow_usd_high is None
        assert estimate.shuffle_usd is None

    def test_pinned_dataflow_usd_for_default_shape(self) -> None:
        # 1000 tiles, n2-standard-4 (4 vCPU / 16 GB @ $0.056 + $0.003557),
        # 4 workers (autoscale target not exceeded), 8 threads/worker,
        # 0.5 s/tile low and 2 s/tile high, +180 s/worker overhead:
        #   low : 4 * (15.625 + 180) / 3600 h * $0.280912/h = $0.06106
        #   high: 4 * (62.5  + 180) / 3600 h * $0.280912/h = $0.07569
        estimate = estimate_cost(_make_config(1000, mode="dataflow"))
        assert estimate.dataflow_usd_low == pytest.approx(0.06106, rel=1e-3)
        assert estimate.dataflow_usd_high == pytest.approx(0.07569, rel=1e-3)

    def test_band_is_ordered(self) -> None:
        estimate = estimate_cost(_make_config(5000, mode="dataflow"))
        assert estimate.dataflow_usd_low is not None
        assert estimate.dataflow_usd_high is not None
        assert 0 < estimate.dataflow_usd_low < estimate.dataflow_usd_high

    def test_max_workers_caps_steady_state(self) -> None:
        # 100k tiles wants ceil(100000 / (8*600)) = 21 workers; capping at
        # 10 changes the estimate (fewer workers, less overhead).
        uncapped = estimate_cost(
            _make_config(1, mode="dataflow", max_workers=100), tile_count=100_000
        )
        capped = estimate_cost(_make_config(1, mode="dataflow", max_workers=10), tile_count=100_000)
        assert uncapped.dataflow_usd_low != capped.dataflow_usd_low


class TestMachineTypeParsing:
    @pytest.mark.parametrize(
        ("machine_type", "vcpus", "memory_gb"),
        [
            ("n2-standard-4", 4, 16.0),
            ("n1-standard-16", 16, 64.0),
            ("e2-standard-8", 8, 32.0),
        ],
    )
    def test_standard_family(self, machine_type: str, vcpus: int, memory_gb: float) -> None:
        parsed_vcpus, parsed_gb, caveat = parse_machine_shape(machine_type)
        assert (parsed_vcpus, parsed_gb) == (vcpus, memory_gb)
        assert caveat is None

    @pytest.mark.parametrize("machine_type", ["n1-highmem-8", "custom-4-16384", "weird"])
    def test_unknown_falls_back_to_n2_standard_4(self, machine_type: str) -> None:
        vcpus, memory_gb, caveat = parse_machine_shape(machine_type)
        assert (vcpus, memory_gb) == (4, 16.0)
        assert caveat is not None and machine_type in caveat

    def test_unknown_machine_type_notes_fallback_in_assumptions(self) -> None:
        estimate = estimate_cost(_make_config(100, mode="dataflow", machine_type="n1-highmem-8"))
        assert any("n1-highmem-8" in a and "n2-standard-4" in a for a in estimate.assumptions)


class TestShuffleModel:
    def test_m6_dataflow_has_shuffle_cost(self) -> None:
        estimate = estimate_cost(_make_config(1000, mode="dataflow", output_tile_size_pixels=2048))
        raw_bytes = 1000 * _TILE_SIZE * _TILE_SIZE * 4  # float32, 1 band
        assert estimate.shuffle_usd == pytest.approx(raw_bytes / 1e9 * DATAFLOW_SHUFFLE_USD_PER_GB)

    def test_non_m6_dataflow_has_no_shuffle(self) -> None:
        estimate = estimate_cost(_make_config(1000, mode="dataflow"))
        assert estimate.shuffle_usd is None

    def test_m6_local_has_no_shuffle(self) -> None:
        estimate = estimate_cost(_make_config(1000, mode="local", output_tile_size_pixels=2048))
        assert estimate.shuffle_usd is None

    def test_output_tile_size_equal_to_compute_is_not_m6(self) -> None:
        estimate = estimate_cost(
            _make_config(1000, mode="dataflow", output_tile_size_pixels=_TILE_SIZE)
        )
        assert estimate.shuffle_usd is None


class TestStorageModel:
    def test_pinned_storage_math(self) -> None:
        estimate = estimate_cost(_make_config(1000))
        raw_bytes = 1000 * _TILE_SIZE * _TILE_SIZE * 4  # float32, 1 band
        expected = raw_bytes * DEFLATE_COMPRESSION_FACTOR / 1e9 * GCS_STORAGE_USD_PER_GB_MONTH
        assert estimate.storage_usd_per_month == pytest.approx(expected)


class TestExternalizedTiles:
    def test_zero_tiles_yields_degenerate_estimate(self) -> None:
        estimate = estimate_cost(_make_config(0, tiles_file="gs://bucket/tiles.ndjson"))
        assert estimate.tile_count == 0
        assert estimate.eecu_seconds_low == 0.0
        assert estimate.eecu_seconds_high == 0.0
        assert estimate.dataflow_usd_low is None
        assert estimate.shuffle_usd is None
        assert estimate.storage_usd_per_month == 0.0
        assert any("externalized" in a for a in estimate.assumptions)

    def test_explicit_tile_count_overrides(self) -> None:
        config = _make_config(0, tiles_file="gs://bucket/tiles.ndjson")
        estimate = estimate_cost(config, tile_count=500)
        assert estimate.tile_count == 500
        assert estimate.eecu_seconds_low == pytest.approx(500 * EECU_SECONDS_PER_TILE_LOW)
        assert estimate.storage_usd_per_month > 0


class TestAssumptions:
    @pytest.mark.parametrize(
        "config_kwargs",
        [
            {"mode": "local"},
            {"mode": "dataflow"},
            {"mode": "dataflow", "output_tile_size_pixels": 2048},
        ],
    )
    def test_assumptions_always_present_and_nonempty(self, config_kwargs: dict) -> None:
        estimate = estimate_cost(_make_config(100, **config_kwargs))
        assert estimate.assumptions
        assert all(isinstance(a, str) and a.strip() for a in estimate.assumptions)

    def test_estimate_is_frozen(self) -> None:
        estimate = estimate_cost(_make_config(100))
        assert isinstance(estimate, CostEstimate)
        with pytest.raises(AttributeError):
            estimate.tile_count = 7  # type: ignore[misc]
