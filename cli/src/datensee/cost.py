"""Pre-submit cost estimation for DatensEE exports.

Pure math over :class:`~datensee.config.PipelineConfig` — no I/O, no
network calls. Produces a :class:`CostEstimate` covering the three cost
surfaces an EE user weighing "leave free batch exports?" cares about:

* **EECU** — Earth Engine compute units burned by the HV API fetches.
  Free for non-commercial EE use; commercial plans bill per EECU-hour.
* **Dataflow** — worker vCPU/memory time (plus Shuffle in two-tier mode).
* **GCS storage** — the monthly carrying cost of the output COGs.

Every constant in the model lives in the block below so the whole
pricing surface is auditable at a glance. All numbers are rough,
order-of-magnitude estimates; EECU usage in particular is dominated by
the (opaque) EE expression, which we deliberately never inspect.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from datensee.config import PipelineConfig

# ---------------------------------------------------------------------------
# Pricing & model constants — as of 2026-07, verify against current pricing.
#
# Dataflow and GCS rates are batch / us-central1 list prices. EE per-tile
# EECU usage is expression-dependent by nature; the range below brackets
# "trivial band math" to "heavy temporal composite" for a 512×512 tile.
# ---------------------------------------------------------------------------

# EE compute: per-tile EECU-seconds range (expression-dependent — wide on
# purpose). Non-commercial EE use is free; commercial EE bills per EECU-hour.
EECU_SECONDS_PER_TILE_LOW: float = 0.2
EECU_SECONDS_PER_TILE_HIGH: float = 5.0
EECU_USD_PER_HOUR_COMMERCIAL: float = 0.40  # informational only, not billed here

# Dataflow batch, us-central1.
DATAFLOW_VCPU_USD_PER_HOUR: float = 0.056
DATAFLOW_MEMORY_USD_PER_GB_HOUR: float = 0.003557
DATAFLOW_SHUFFLE_USD_PER_GB: float = 0.011

# Wall-time model: tile fetches are I/O-bound at ~1 tile/second/harness
# thread; the low/high band applies a ±2x factor to that per-tile second.
SECONDS_PER_TILE_PER_THREAD: float = 1.0
PER_TILE_SECONDS_BAND: float = 2.0  # low = nominal / band, high = nominal * band
# Batch autoscaler assumed to target ~10 minutes of steady-state work.
AUTOSCALE_TARGET_SECONDS: float = 600.0
# Fixed per-worker boot + teardown overhead.
WORKER_OVERHEAD_SECONDS: float = 180.0

# Machine-type model: the *-standard-N families (n1/n2/e2) all ship
# 4 GB of memory per vCPU. Unknown machine types fall back to this shape.
MEMORY_GB_PER_VCPU: float = 4.0
FALLBACK_MACHINE_TYPE: str = "n2-standard-4"
FALLBACK_VCPUS: int = 4

# GCS standard storage, plus the deflate compression factor we assume for
# COG output (raster imagery typically deflates to ~50% of raw).
GCS_STORAGE_USD_PER_GB_MONTH: float = 0.02
DEFLATE_COMPRESSION_FACTOR: float = 0.5

# Bytes per pixel by output data type (mirrors datensee.config._BYTES_PER_PIXEL).
_BYTES_PER_PIXEL: dict[str, int] = {
    "float32": 4,
    "float64": 8,
    "int16": 2,
    "int32": 4,
    "uint8": 1,
    "uint16": 2,
}

_GB: float = 1e9  # decimal gigabyte, matching GCP billing units

_STANDARD_MACHINE_RE = re.compile(r"-standard-(\d+)$")


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """Pre-submit cost estimate for one export.

    All dollar figures are USD. ``None`` means "not applicable", not
    "zero": ``dataflow_usd_*`` is ``None`` in local-runner mode and
    ``shuffle_usd`` is ``None`` unless the job runs two-tier
    assembly on Dataflow.

    Attributes:
        tile_count: Compute tiles the estimate is based on (0 when the
            tile grid is externalized to a file and no explicit count
            was supplied — every other figure is then zero/None).
        eecu_seconds_low: Low end of the EECU-seconds range.
        eecu_seconds_high: High end of the EECU-seconds range.
        dataflow_usd_low: Low end of Dataflow worker cost, or None.
        dataflow_usd_high: High end of Dataflow worker cost, or None.
        shuffle_usd: Dataflow Shuffle cost (two-tier + Dataflow only), or None.
        storage_usd_per_month: Monthly GCS carrying cost of the output.
        assumptions: One-line statements of every non-obvious modeling
            assumption baked into the numbers above.
    """

    tile_count: int
    eecu_seconds_low: float
    eecu_seconds_high: float
    dataflow_usd_low: float | None
    dataflow_usd_high: float | None
    shuffle_usd: float | None
    storage_usd_per_month: float
    assumptions: list[str]

    @property
    def eecu_hours_low(self) -> float:
        """Low end of the EECU range in EECU-hours."""
        return self.eecu_seconds_low / 3600.0

    @property
    def eecu_hours_high(self) -> float:
        """High end of the EECU range in EECU-hours."""
        return self.eecu_seconds_high / 3600.0


def parse_machine_shape(machine_type: str) -> tuple[int, float, str | None]:
    """Derive (vCPUs, memory GB, caveat) from a GCE machine type name.

    Handles the ``*-standard-N`` family (n1/n2/e2, which all provision
    4 GB per vCPU). Anything else falls back to the shape of
    ``n2-standard-4`` with an explanatory caveat.

    Args:
        machine_type: GCE machine type, e.g. ``"n2-standard-4"``.

    Returns:
        Tuple of (vcpus, memory_gb, caveat) where caveat is a one-line
        assumption string, or None when the type parsed cleanly.
    """
    match = _STANDARD_MACHINE_RE.search(machine_type)
    if match:
        vcpus = int(match.group(1))
        return vcpus, vcpus * MEMORY_GB_PER_VCPU, None
    return (
        FALLBACK_VCPUS,
        FALLBACK_VCPUS * MEMORY_GB_PER_VCPU,
        f"machine type '{machine_type}' not recognized; "
        f"priced as {FALLBACK_MACHINE_TYPE} ({FALLBACK_VCPUS} vCPU, "
        f"{FALLBACK_VCPUS * MEMORY_GB_PER_VCPU:.0f} GB)",
    )


def _raw_output_bytes(config: PipelineConfig, tile_count: int) -> int:
    """Uncompressed output bytes for ``tile_count`` compute tiles."""
    bytes_per_pixel = _BYTES_PER_PIXEL.get(config.output.data_type, 4)
    edge = config.tile_grid.tile_size_pixels
    return tile_count * edge * edge * bytes_per_pixel * config.output.band_count


def _dataflow_worker_usd(
    tile_count: int,
    *,
    vcpus: int,
    memory_gb: float,
    num_workers: int,
    max_workers: int,
    harness_threads: int,
    seconds_per_tile: float,
) -> float:
    """Worker vCPU+memory USD for one point of the per-tile-seconds band.

    Steady-state worker count assumes the batch autoscaler targets
    ~``AUTOSCALE_TARGET_SECONDS`` of remaining work per thread, floored
    at the configured initial worker count. Each worker is billed for
    the fetch wall time plus fixed boot/teardown overhead.
    """
    steady_workers = max(
        num_workers,
        min(max_workers, math.ceil(tile_count / (harness_threads * AUTOSCALE_TARGET_SECONDS))),
    )
    fetch_wall_seconds = tile_count * seconds_per_tile / (steady_workers * harness_threads)
    worker_hours = steady_workers * (fetch_wall_seconds + WORKER_OVERHEAD_SECONDS) / 3600.0
    hourly_rate = vcpus * DATAFLOW_VCPU_USD_PER_HOUR + memory_gb * DATAFLOW_MEMORY_USD_PER_GB_HOUR
    return worker_hours * hourly_rate


def estimate_cost(config: PipelineConfig, *, tile_count: int | None = None) -> CostEstimate:
    """Estimate EECU, Dataflow, and storage cost for an export config.

    Pure function — reads the config, does arithmetic, returns. See the
    constants block at the top of this module for the full pricing model.

    Args:
        config: Validated pipeline configuration.
        tile_count: Explicit tile count override. Required to get a
            non-degenerate estimate when the tile grid is externalized
            to a file (``config.tile_count`` is 0 in that case).

    Returns:
        A :class:`CostEstimate`. When the tile count is unknown (0),
        all figures are zero/None and ``assumptions`` says why.
    """
    tiles = tile_count if tile_count is not None else config.tile_count

    if tiles <= 0:
        return CostEstimate(
            tile_count=0,
            eecu_seconds_low=0.0,
            eecu_seconds_high=0.0,
            dataflow_usd_low=None,
            dataflow_usd_high=None,
            shuffle_usd=None,
            storage_usd_per_month=0.0,
            assumptions=[
                "tile count unknown (tiles externalized to a file); "
                "pass an explicit tile_count to estimate_cost() for numbers"
            ],
        )

    assumptions: list[str] = [
        f"EECU per tile assumed {EECU_SECONDS_PER_TILE_LOW:g}-"
        f"{EECU_SECONDS_PER_TILE_HIGH:g} EECU-seconds; actual usage is "
        "expression-dependent",
        "non-commercial EE use is free; commercial EE bills "
        f"~${EECU_USD_PER_HOUR_COMMERCIAL:.2f}/EECU-hour",
        "pricing constants are batch/us-central1 list prices as of 2026-07; "
        "verify against current pricing",
    ]

    eecu_low = tiles * EECU_SECONDS_PER_TILE_LOW
    eecu_high = tiles * EECU_SECONDS_PER_TILE_HIGH

    raw_bytes = _raw_output_bytes(config, tiles)
    storage_usd = raw_bytes * DEFLATE_COMPRESSION_FACTOR / _GB * GCS_STORAGE_USD_PER_GB_MONTH
    assumptions.append(
        f"storage assumes deflate compresses raster output to "
        f"~{DEFLATE_COMPRESSION_FACTOR:.0%} of raw size"
    )

    is_dataflow = config.runner.mode == "dataflow" and config.runner.dataflow is not None
    out_size = config.output.output_tile_size_pixels
    is_m6 = out_size is not None and out_size > config.tile_grid.tile_size_pixels

    dataflow_low: float | None = None
    dataflow_high: float | None = None
    shuffle_usd: float | None = None

    if is_dataflow:
        dataflow = config.runner.dataflow
        assert dataflow is not None  # narrowed by is_dataflow
        vcpus, memory_gb, machine_caveat = parse_machine_shape(dataflow.machine_type)
        if machine_caveat is not None:
            assumptions.append(machine_caveat)
        else:
            assumptions.append(
                f"memory assumed {MEMORY_GB_PER_VCPU:g} GB/vCPU for {dataflow.machine_type}"
            )

        def worker_usd(seconds_per_tile: float) -> float:
            return _dataflow_worker_usd(
                tiles,
                vcpus=vcpus,
                memory_gb=memory_gb,
                num_workers=dataflow.num_workers,
                max_workers=dataflow.max_workers,
                harness_threads=dataflow.number_of_worker_harness_threads,
                seconds_per_tile=seconds_per_tile,
            )

        dataflow_low = worker_usd(SECONDS_PER_TILE_PER_THREAD / PER_TILE_SECONDS_BAND)
        dataflow_high = worker_usd(SECONDS_PER_TILE_PER_THREAD * PER_TILE_SECONDS_BAND)
        assumptions.append(
            f"fetch throughput assumed ~{SECONDS_PER_TILE_PER_THREAD:g} "
            f"tile/second/harness-thread (x{PER_TILE_SECONDS_BAND:g} band each way)"
        )
        assumptions.append(
            f"autoscaler assumed to target ~{AUTOSCALE_TARGET_SECONDS / 60:.0f} min "
            f"of steady-state work, plus {WORKER_OVERHEAD_SECONDS / 60:.0f} min/worker "
            "boot/teardown overhead"
        )

        if is_m6:
            shuffle_usd = raw_bytes / _GB * DATAFLOW_SHUFFLE_USD_PER_GB
            assumptions.append(
                "two-tier assembly shuffles the full uncompressed output "
                "through Dataflow Shuffle (GroupByKey)"
            )
    else:
        assumptions.append("local runner: no Dataflow cost; your machine does the work")

    return CostEstimate(
        tile_count=tiles,
        eecu_seconds_low=eecu_low,
        eecu_seconds_high=eecu_high,
        dataflow_usd_low=dataflow_low,
        dataflow_usd_high=dataflow_high,
        shuffle_usd=shuffle_usd,
        storage_usd_per_month=storage_usd,
        assumptions=assumptions,
    )
