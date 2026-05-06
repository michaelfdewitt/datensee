"""Pydantic models for pipeline configuration.

This module defines the contract between the Python CLI and the Java Beam pipeline.
The serialized form matches pipeline-config.schema.json in /contract.

The canonical export shape is :class:`PixelGrid` — CRS code + 6-tuple affine
transform + integer dimensions. It mirrors Earth Engine's own ``PixelGrid``
type so the parent grid (and per-tile sub-grids derived from it) can be sent
verbatim to the HV ``computePixels`` endpoint.

Tile geometry inside the export is integer pixel rectangles
(``col_px``/``row_px``/``width_px``/``height_px``) within that parent grid.
Float bboxes are derived from ``transform × pixel_offsets`` — never persisted —
so cross-export grid alignment is unconditional whenever two exports share
the same CRS, scale, and tile size.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

_BYTES_PER_PIXEL: dict[str, int] = {
    "float32": 4,
    "float64": 8,
    "int16": 2,
    "int32": 4,
    "uint8": 1,
    "uint16": 2,
}


class GridDimensions(BaseModel):
    """Pixel dimensions of a grid (mirrors EE's ``GridDimensions``)."""

    width: int = Field(gt=0)
    height: int = Field(gt=0)


class AffineTransform(BaseModel):
    """6-tuple geo-affine in the EE / GDAL / GeoTIFF convention.

    For axis-aligned grids (always, in our use), ``shear_x`` and ``shear_y``
    are zero, ``scale_x`` is positive, and ``scale_y`` is **negative** —
    that puts ``(translate_x, translate_y)`` at the NW corner of the
    top-left pixel (PixelIsArea: pixel ``(u=0, v=0)`` is the *corner*,
    not the centre).
    """

    scale_x: float
    shear_x: float = 0.0
    translate_x: float
    shear_y: float = 0.0
    scale_y: float
    translate_y: float


class PixelGrid(BaseModel):
    """Canonical export grid: CRS + affine + integer dimensions.

    Sent verbatim to EE's ``computePixels`` endpoint at fetch time.
    """

    crs_code: str = Field(description="EPSG code or proj string, e.g. 'EPSG:4326'")
    affine_transform: AffineTransform
    dimensions: GridDimensions


class TileCoordinate(BaseModel):
    """A compute tile as an integer pixel rectangle inside the parent grid.

    ``col_px``/``row_px`` are **local** offsets from the parent grid's
    ``translate_x``/``translate_y`` — not absolute against a global ``(0, 0)``.
    The parent's translate already encodes where the export sits in CRS units;
    tile offsets within it are small. ``width_px``/``height_px`` are the tile's
    own pixel dimensions; for root tiles they equal ``tile_size_pixels``, and
    quadtree split children halve each axis.

    ``row`` and ``col`` are the compute-tile indices within the export bbox
    (row=0 is the northernmost tile, col=0 the westernmost). They stay pinned
    to the *root* compute tile — split children inherit them so the failure
    journal can attribute children to the parent that originated them.

    ``out_row``/``out_col`` (M6 two-tier tiling) identify the output tile this
    compute tile belongs to. When two-tier mode is disabled they equal
    ``row``/``col``.

    ``lineage`` records the quadtree path from the root compute tile down to a
    sub-tile. Each entry is a quadrant index 0–3, layout-independent of CRS
    axis order: ``0=x-low/y-low``, ``1=x-high/y-low``, ``2=x-low/y-high``,
    ``3=x-high/y-high``. Empty list = root compute tile. Lineage exists for
    the failure-journal / retry-decision logic.
    """

    col_px: int = Field(ge=0)
    row_px: int = Field(ge=0)
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    row: int = 0
    col: int = 0
    out_row: int = 0
    out_col: int = 0
    lineage: list[int] = Field(
        default_factory=list,
        description="Quadtree path from root compute tile (each entry 0–3).",
    )


class TileGrid(BaseModel):
    """Tile decomposition of the export region.

    Carries the parent :class:`PixelGrid` plus the tile size and either inline
    tiles or a path to an NDJSON tile file.
    """

    pixel_grid: PixelGrid
    tile_size_pixels: int = Field(default=512, gt=0, description="Tile edge length in pixels")
    tiles: list[TileCoordinate] | None = Field(
        default=None,
        description="Inline tile coordinates (mutually exclusive with tiles_file)",
    )
    tiles_file: str | None = Field(
        default=None,
        description="GCS URI or local path to NDJSON file of tile coordinates",
    )

    @property
    def crs(self) -> str:
        """CRS string — delegates to ``pixel_grid.crs_code``."""
        return self.pixel_grid.crs_code

    @property
    def pixel_size(self) -> float:
        """Pixel size in CRS units (== ``affine_transform.scale_x``)."""
        return self.pixel_grid.affine_transform.scale_x

    @model_validator(mode="after")
    def exactly_one_tile_source(self) -> TileGrid:
        has_inline = self.tiles is not None and len(self.tiles) > 0
        has_file = self.tiles_file is not None and self.tiles_file.strip() != ""
        if not has_inline and not has_file:
            raise ValueError("TileGrid must have either inline tiles or a tiles_file path")
        if has_inline and has_file:
            raise ValueError(
                "TileGrid cannot have both inline tiles and tiles_file — use one or the other"
            )
        return self


class CogParameters(BaseModel):
    """Cloud Optimized GeoTIFF output parameters."""

    overview_levels: list[int] = Field(default=[2, 4, 8, 16, 32])
    blocksize: int = Field(default=512, gt=0)
    compress: Literal["deflate", "none"] = "deflate"
    predictor: Literal[1, 2, 3] = Field(
        default=2,
        description="1=none, 2=horizontal (int), 3=floating-point",
    )


class OutputConfig(BaseModel):
    """Destination and format config for pipeline output.

    output_path accepts either a GCS URI (gs://bucket/prefix) or a local
    directory path for local-runner mode.

    output_tile_size_pixels (M6 two-tier tiling) specifies the edge length
    of the *output* COGs. When unset or equal to the compute tile size
    (`tile_grid.tile_size_pixels`), each compute tile becomes its own
    output COG. When set to a multiple of the compute tile size, compute
    tiles are grouped and assembled into larger output COGs whose
    internal block size is the compute tile size. This decouples fetch
    parallelism from output file granularity.
    """

    output_path: str = Field(description="Output path: GCS URI (gs://…) or local directory")
    band_count: int = Field(default=1, gt=0, description="Number of output bands")
    data_type: Literal["float32", "float64", "int16", "int32", "uint8", "uint16"] = Field(
        default="float32", description="Pixel data type for output raster"
    )
    output_tile_size_pixels: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Edge length of output tile COGs in pixels. Must be a multiple of "
            "tile_grid.tile_size_pixels. Defaults to tile_size_pixels (one COG per "
            "compute tile)."
        ),
    )
    cog: CogParameters = Field(default_factory=CogParameters)


class RateLimitConfig(BaseModel):
    """Advisory rate-limit config. **Currently not enforced by the pipeline.**

    Tile fetches are I/O-bound; the binding throughput constraint is the
    count of concurrent in-flight HTTP requests we keep open, not a
    project-wide QPS budget. EE's HV API enforces its own quota and
    surfaces overflow as 429 — ``TileFetchDoFn``'s exponential-backoff
    retry path is the rate-shaping signal. A client-side QPS limiter
    sized below EE's actual capacity just starves Dataflow's autoscaler
    and slows the export.

    The field stays in the schema for backwards compatibility with
    callers that pass ``max_qps`` and to keep the user-stated intent
    visible in ``_pipeline-config.json`` for diagnostics, but neither
    the Java pipeline nor the Python CLI consults it at runtime.
    """

    max_qps: int = Field(
        default=100,
        gt=0,
        description=(
            "Advisory only — see class docstring. The pipeline does not "
            "enforce a per-worker token budget; throughput is shaped by "
            "Dataflow worker parallelism plus 429-driven exponential "
            "backoff."
        ),
    )


class DataflowRunnerConfig(BaseModel):
    """Dataflow-specific runner options.

    The defaults are tuned for I/O-bound tile fetches against the EE HV
    API: every knob below ratchets actual concurrency upward, because
    the binding throughput constraint is *concurrent in-flight requests*
    (workers × harness threads × HTTP latency), not project-wide QPS.
    EE's own quota system, surfaced as 429s with exponential-backoff
    retries inside ``TileFetchDoFn``, is the rate-shaping signal —
    Dataflow-side rate limiters only starve us.
    """

    project: str
    region: str
    temp_location: str = Field(description="GCS URI for Dataflow temp files")
    staging_location: str = Field(description="GCS URI for Dataflow staging files")
    machine_type: str = "n2-standard-4"
    num_workers: int = Field(
        default=4,
        gt=0,
        description=(
            "Initial worker count. Dataflow's batch autoscaler is reactive — "
            "it only scales up after observing backlog — so booting with a "
            "non-trivial number gets the pipeline to steady-state throughput "
            "much faster than starting from 1."
        ),
    )
    max_workers: int = Field(default=100, gt=0)
    autoscaling_algorithm: Literal["THROUGHPUT_BASED", "NONE"] = Field(
        default="THROUGHPUT_BASED",
        description=(
            "Dataflow autoscaling mode. Default 'THROUGHPUT_BASED' (the only "
            "useful choice for batch). Flex Template launches sometimes "
            "default to 'NONE' depending on Beam version; we pin "
            "'THROUGHPUT_BASED' so behavior doesn't drift with the runner."
        ),
    )
    number_of_worker_harness_threads: int = Field(
        default=8,
        gt=0,
        description=(
            "Per-worker fetcher concurrency. Beam's default sizes against "
            "vCPU count, which is wrong for I/O-bound work — each harness "
            "thread blocks on a ~1s HTTP round-trip, so we want more "
            "threads than vCPUs. We default to 8 (2× n2-standard-4 vCPUs) "
            "to stay inside typical EE HV project-quota concurrency caps; "
            "projects with paid EE quota can crank this higher to push "
            "more tiles in flight per worker."
        ),
    )
    service_account_email: str | None = None
    network: str | None = None
    subnetwork: str | None = None
    labels: dict[str, str] | None = Field(
        default=None,
        description=(
            "Dataflow job labels, forwarded as --labels=JSON to the pipeline. "
            "Useful for filtering jobs.list queries by caller / integration."
        ),
    )


class RunnerConfig(BaseModel):
    """Runner selection: Dataflow or local direct runner."""

    mode: Literal["dataflow", "local"] = "local"
    dataflow: DataflowRunnerConfig | None = None

    @model_validator(mode="after")
    def dataflow_config_required_for_dataflow_mode(self) -> RunnerConfig:
        if self.mode == "dataflow" and self.dataflow is None:
            raise ValueError("dataflow config is required when mode='dataflow'")
        return self


class PipelineConfig(BaseModel):
    """Top-level pipeline configuration passed from CLI to Beam pipeline.

    The ee_expression field is opaque — the CLI never interprets it.
    Earth Engine evaluates it per-tile via the High Volume API.
    """

    ee_expression: str = Field(description="Serialized EE computation (opaque JSON string)")
    gee_project: str = Field(
        description="GCP project ID with Earth Engine API enabled (used in HV API URL)"
    )
    tile_grid: TileGrid
    output: OutputConfig
    runner: RunnerConfig = Field(default_factory=RunnerConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    snapshot_time: int | None = Field(
        default=None,
        description=(
            "Unix nanos at which the EE expression's asset references "
            "were pinned (snapshot consistency across parallel tile "
            "fetches). Set by api.export(); read by api.retry() to pin "
            "split children to the same snapshot as their parents."
        ),
    )

    @property
    def tile_count(self) -> int:
        """Number of tiles (0 when tiles are externalized to a file)."""
        return len(self.tile_grid.tiles) if self.tile_grid.tiles is not None else 0

    @property
    def raw_output_bytes(self) -> int:
        """Exact uncompressed output size in bytes."""
        bpp = _BYTES_PER_PIXEL.get(self.output.data_type, 4)
        px = self.tile_grid.tile_size_pixels
        return self.tile_count * px * px * bpp * self.output.band_count

    @model_validator(mode="after")
    def ee_expression_is_valid_json(self) -> PipelineConfig:
        try:
            json.loads(self.ee_expression)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"ee_expression must be a valid JSON string: {exc}") from exc
        return self

    @model_validator(mode="after")
    def output_tile_size_must_be_multiple_of_compute_tile_size(self) -> PipelineConfig:
        out_size = self.output.output_tile_size_pixels
        if out_size is None:
            return self
        compute_size = self.tile_grid.tile_size_pixels
        if out_size % compute_size != 0:
            raise ValueError(
                f"output.output_tile_size_pixels ({out_size}) must be a multiple of "
                f"tile_grid.tile_size_pixels ({compute_size}). Got remainder "
                f"{out_size % compute_size}."
            )
        if out_size < compute_size:
            raise ValueError(
                f"output.output_tile_size_pixels ({out_size}) must be >= "
                f"tile_grid.tile_size_pixels ({compute_size})."
            )
        return self

    @property
    def effective_output_tile_size_pixels(self) -> int:
        """Output COG edge length: either the configured value or the compute tile size."""
        return self.output.output_tile_size_pixels or self.tile_grid.tile_size_pixels

    @property
    def expected_output_tile_count(self) -> int:
        """Number of output COG files the pipeline will produce.

        Equal to the compute tile count except in M6 two-tier mode, where
        compute tiles are grouped by ``(out_row, out_col)`` and one COG is
        written per group. Zero when tiles are externalized to a file.
        """
        if self.tile_grid.tiles is None:
            return 0
        if self.output.output_tile_size_pixels is None:
            return self.tile_count
        return len({(t.out_row, t.out_col) for t in self.tile_grid.tiles})

    def write_json(self, path: Path) -> None:
        """Serialize config to JSON file for handoff to the Java pipeline."""
        path.write_text(self.model_dump_json(indent=2, exclude_none=True))

    @classmethod
    def read_json(cls, path: Path) -> PipelineConfig:
        """Deserialize config from a JSON file."""
        return cls.model_validate_json(path.read_text())
