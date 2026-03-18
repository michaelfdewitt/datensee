"""Cost estimation for pipeline runs.

Pure functions that estimate wall time, EECU consumption, Dataflow compute
costs, and storage costs from pipeline configuration. All estimates are
best-effort — EE expression complexity is unknowable ahead of time, so EECU
is presented as a range.
"""

from __future__ import annotations

from pydantic import BaseModel

from datensee.config import PipelineConfig


class CostEstimate(BaseModel):
    """Pre-submission cost estimate for a pipeline run."""

    tile_count: int
    estimated_wall_seconds: float

    # EECU (range — expression complexity is unpredictable)
    eecu_seconds_low: float
    eecu_seconds_typical: float
    eecu_seconds_high: float

    # Dataflow (None for local mode)
    dataflow_vcpu_hours: float | None = None
    dataflow_memory_gb_hours: float | None = None
    dataflow_cost_usd: float | None = None

    # Storage
    output_size_bytes: int
    storage_cost_usd_per_month: float


# ---------------------------------------------------------------------------
# Machine specs (n2-standard family)
# ---------------------------------------------------------------------------

_N2_STANDARD_SPECS: dict[str, tuple[int, float]] = {
    "n2-standard-2": (2, 8.0),
    "n2-standard-4": (4, 16.0),
    "n2-standard-8": (8, 32.0),
    "n2-standard-16": (16, 64.0),
    "n2-standard-32": (32, 128.0),
    "n2-standard-48": (48, 192.0),
    "n2-standard-64": (64, 256.0),
    "n2-standard-96": (96, 384.0),
}


def _machine_specs(machine_type: str) -> tuple[int, float]:
    """Return (vCPUs, memory_gb) for a machine type."""
    if machine_type in _N2_STANDARD_SPECS:
        return _N2_STANDARD_SPECS[machine_type]
    # Fallback: parse "n2-standard-N" pattern
    parts = machine_type.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        vcpus = int(parts[1])
        return (vcpus, vcpus * 4.0)
    # Unknown machine type — conservative default
    return (4, 16.0)


# ---------------------------------------------------------------------------
# Dataflow regional pricing (USD per vCPU-hour / GB-hour)
# https://cloud.google.com/dataflow/pricing
# ---------------------------------------------------------------------------

_DATAFLOW_RATES: dict[str, tuple[float, float]] = {
    "us-central1": (0.056, 0.003557),
    "us-east1": (0.056, 0.003557),
    "us-west1": (0.056, 0.003557),
    "europe-west1": (0.0616, 0.003913),
    "europe-west2": (0.0721, 0.004579),
    "asia-east1": (0.0656, 0.004163),
    "asia-southeast1": (0.0692, 0.004392),
}

_DEFAULT_RATES = (0.056, 0.003557)  # us-central1 fallback


def _dataflow_regional_rates(region: str) -> tuple[float, float]:
    """Return (vcpu_hour_usd, gb_hour_usd) for a Dataflow region."""
    return _DATAFLOW_RATES.get(region, _DEFAULT_RATES)


# ---------------------------------------------------------------------------
# Bytes per pixel
# ---------------------------------------------------------------------------

_BYTES_PER_PIXEL: dict[str, int] = {
    "float32": 4,
    "float64": 8,
    "int16": 2,
    "int32": 4,
    "uint8": 1,
    "uint16": 2,
}


def _bytes_per_pixel(data_type: str) -> int:
    """Return raw (uncompressed) bytes per pixel for a data type."""
    return _BYTES_PER_PIXEL.get(data_type, 4)


# GCS standard storage: $0.020/GB/month (us multi-region)
_GCS_STORAGE_USD_PER_BYTE_MONTH = 0.020 / (1024**3)


def estimate_cost(
    config: PipelineConfig,
    *,
    eecu_per_tile: float = 1.0,
) -> CostEstimate:
    """Estimate costs for a pipeline run.

    Args:
        config: Validated pipeline configuration.
        eecu_per_tile: Baseline EECU-seconds per tile (from calibration runs).
            Low/typical/high are 1x/3x/10x this value.

    Returns:
        CostEstimate with all fields populated.
    """
    tile_count = _tile_count(config)
    max_qps = config.rate_limit.max_qps

    # Wall time: tiles / (qps × efficiency factor)
    wall_seconds = tile_count / (max_qps * 0.7) if max_qps > 0 else 0.0

    # EECU range
    eecu_low = tile_count * eecu_per_tile
    eecu_typical = tile_count * eecu_per_tile * 3.0
    eecu_high = tile_count * eecu_per_tile * 10.0

    # Storage
    bpp = _bytes_per_pixel(config.output.data_type)
    tile_px = config.tile_grid.tile_size_pixels
    raw_bytes = tile_count * tile_px * tile_px * bpp * config.output.band_count
    # LZW typically compresses to ~40-60% of raw; use 50%.
    estimated_bytes = int(raw_bytes * 0.5)
    storage_cost = estimated_bytes * _GCS_STORAGE_USD_PER_BYTE_MONTH

    # Dataflow costs
    dataflow_vcpu_hours: float | None = None
    dataflow_memory_gb_hours: float | None = None
    dataflow_cost_usd: float | None = None

    if config.runner.mode == "dataflow" and config.runner.dataflow is not None:
        df = config.runner.dataflow
        vcpus, memory_gb = _machine_specs(df.machine_type)
        vcpu_rate, gb_rate = _dataflow_regional_rates(df.region)

        # Assume workers scale up to max_workers for the duration.
        # Actual cost is lower for short jobs due to ramp-up.
        wall_hours = wall_seconds / 3600.0
        dataflow_vcpu_hours = vcpus * df.max_workers * wall_hours
        dataflow_memory_gb_hours = memory_gb * df.max_workers * wall_hours
        dataflow_cost_usd = dataflow_vcpu_hours * vcpu_rate + dataflow_memory_gb_hours * gb_rate

    return CostEstimate(
        tile_count=tile_count,
        estimated_wall_seconds=wall_seconds,
        eecu_seconds_low=eecu_low,
        eecu_seconds_typical=eecu_typical,
        eecu_seconds_high=eecu_high,
        dataflow_vcpu_hours=dataflow_vcpu_hours,
        dataflow_memory_gb_hours=dataflow_memory_gb_hours,
        dataflow_cost_usd=dataflow_cost_usd,
        output_size_bytes=estimated_bytes,
        storage_cost_usd_per_month=storage_cost,
    )


def _tile_count(config: PipelineConfig) -> int:
    """Extract tile count from config (inline tiles or tiles_file)."""
    if config.tile_grid.tiles is not None:
        return len(config.tile_grid.tiles)
    # When tiles are externalized, we don't know the count without reading the file.
    # Return 0 — caller should have the count from decompose_region.
    return 0
