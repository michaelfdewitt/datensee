"""Pydantic models for pipeline configuration.

This module defines the contract between the Python CLI and the Java Beam pipeline.
The serialized form matches pipeline-config.schema.json in /contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class TileCoordinate(BaseModel):
    """A single tile's bounding box in the target CRS."""

    x_min: float
    y_min: float
    x_max: float
    y_max: float
    row: int
    col: int


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
    compress: Literal["lzw", "deflate", "zstd", "none"] = "lzw"
    predictor: Literal[1, 2, 3] = Field(
        default=2,
        description="1=none, 2=horizontal (int), 3=floating-point",
    )


class OutputConfig(BaseModel):
    """Destination and format config for pipeline output.

    output_path accepts either a GCS URI (gs://bucket/prefix) or a local
    directory path for local-runner mode.
    """

    output_path: str = Field(description="Output path: GCS URI (gs://…) or local directory")
    band_count: int = Field(default=1, gt=0, description="Number of output bands")
    data_type: Literal["float32", "float64", "int16", "int32", "uint8", "uint16"] = Field(
        default="float32", description="Pixel data type for output raster"
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

    @model_validator(mode="after")
    def ee_expression_is_valid_json(self) -> PipelineConfig:
        try:
            json.loads(self.ee_expression)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"ee_expression must be a valid JSON string: {exc}") from exc
        return self

    def write_json(self, path: Path) -> None:
        """Serialize config to JSON file for handoff to the Java pipeline."""
        path.write_text(self.model_dump_json(indent=2, exclude_none=True))

    @classmethod
    def read_json(cls, path: Path) -> PipelineConfig:
        """Deserialize config from a JSON file."""
        return cls.model_validate_json(path.read_text())
