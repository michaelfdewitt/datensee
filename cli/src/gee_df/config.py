"""Pydantic models for pipeline configuration.

This module defines the contract between the Python CLI and the Java Beam pipeline.
The serialized form matches pipeline-config.schema.json in /contract.
"""

from __future__ import annotations

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
    tiles: list[TileCoordinate]

    @model_validator(mode="after")
    def tiles_not_empty(self) -> TileGrid:
        if not self.tiles:
            raise ValueError("TileGrid must contain at least one tile")
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

    output_path: str = Field(
        description="Output path: GCS URI (gs://…) or local directory"
    )
    cog: CogParameters = Field(default_factory=CogParameters)


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

    ee_expression: str = Field(
        description="Serialized EE computation (opaque JSON string)"
    )
    gee_project: str = Field(
        description="GCP project ID with Earth Engine API enabled (used in HV API URL)"
    )
    tile_grid: TileGrid
    output: OutputConfig
    runner: RunnerConfig = Field(default_factory=RunnerConfig)

    def write_json(self, path: Path) -> None:
        """Serialize config to JSON file for handoff to the Java pipeline."""
        path.write_text(self.model_dump_json(indent=2))

    @classmethod
    def read_json(cls, path: Path) -> PipelineConfig:
        """Deserialize config from a JSON file."""
        return cls.model_validate_json(path.read_text())
