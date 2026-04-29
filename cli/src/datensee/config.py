"""Pydantic models for pipeline configuration.

This module defines the contract between the Python CLI and the Java Beam pipeline.
The serialized form matches pipeline-config.schema.json in /contract.
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


class TileCoordinate(BaseModel):
    """A single compute tile's bounding box in the target CRS.

    `row` and `col` are the compute-tile indices within the export bbox
    and stay pinned to the *root* compute tile — they do not change when
    a tile is split adaptively. `out_row` / `out_col` (M6 two-tier
    tiling) identify the output tile this compute tile belongs to;
    when two-tier tiling is disabled they equal `row` and `col`.

    `lineage` (adaptive quadtree retry — sketch only at present) records
    the path from the root compute tile down to a sub-tile. Each entry
    is a quadrant index 0–3, layout-independent of CRS axis order:
    ``0=x-low/y-low, 1=x-high/y-low, 2=x-low/y-high, 3=x-high/y-high``.
    Empty list = root compute tile (the common case). Lineage is
    informational on the success path — the bounding box is the
    geometric truth the assembler keys on; lineage exists for the
    failure-journal / retry-decision logic.
    """

    x_min: float
    y_min: float
    x_max: float
    y_max: float
    row: int
    col: int
    out_row: int = 0
    out_col: int = 0
    lineage: list[int] = Field(
        default_factory=list,
        description="Quadtree path from root compute tile (each entry 0–3).",
    )


class TileGrid(BaseModel):
    """Tile decomposition of the export region."""

    crs: str = Field(description="EPSG code or proj string, e.g. 'EPSG:4326'")
    scale_meters: float = Field(gt=0, description="Pixel size in meters at the native CRS")
    tile_size_pixels: int = Field(default=512, gt=0, description="Tile edge length in pixels")
    tiles: list[TileCoordinate] | None = Field(
        default=None,
        description="Inline tile coordinates (mutually exclusive with tiles_file)",
    )
    tiles_file: str | None = Field(
        default=None,
        description="GCS URI or local path to NDJSON file of tile coordinates",
    )

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
    """Rate limiting for the EE High Volume API."""

    max_qps: int = Field(
        default=100,
        gt=0,
        description="Maximum queries per second across all workers",
    )


class DataflowRunnerConfig(BaseModel):
    """Dataflow-specific runner options."""

    project: str
    region: str
    temp_location: str = Field(description="GCS URI for Dataflow temp files")
    staging_location: str = Field(description="GCS URI for Dataflow staging files")
    machine_type: str = "n2-standard-4"
    max_workers: int = Field(default=100, gt=0)
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
