"""Pydantic models for pipeline configuration.

Defines the runner-agnostic envelope for the contract between the
Python CLI and the Java Beam pipeline: submission, auth, and snapshot
fields, plus the discriminated payload describing the work.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# Pixel-shaped models live in datensee.pixel.config. Re-exported here so
# `from datensee.config import PixelGrid, TileGrid, ...` keeps working
# across existing tests and call sites without churning imports.
from datensee.pixel.config import (
    AffineTransform,
    GridDimensions,
    OutputConfig,
    PixelGrid,
    PixelPayload,
    TileCoordinate,
    TileGrid,
)

__all__ = [
    "AffineTransform",
    "DataflowRunnerConfig",
    "GridDimensions",
    "OutputConfig",
    "PipelineConfig",
    "PixelGrid",
    "PixelPayload",
    "RunnerConfig",
    "TileCoordinate",
    "TileGrid",
]

_BYTES_PER_PIXEL: dict[str, int] = {
    "float32": 4,
    "float64": 8,
    "int16": 2,
    "int32": 4,
    "uint8": 1,
    "uint16": 2,
}


class DataflowRunnerConfig(BaseModel):
    """Dataflow-specific execution parameters."""

    project: str = Field(description="GCP project ID")
    region: str = Field(description="Dataflow region (e.g. 'us-central1')")
    temp_location: str = Field(description="GCS URI for Dataflow temp files")
    staging_location: str = Field(description="GCS URI for Dataflow staging files")
    machine_type: str = "n2-standard-4"
    num_workers: int = Field(
        default=4,
        gt=0,
        description=(
            "Initial worker count. Dataflow's batch autoscaler is reactive, "
            "so booting with a non-trivial number gets the pipeline to steady "
            "throughput faster than starting from 1."
        ),
    )
    max_workers: int = Field(default=100, gt=0)
    autoscaling_algorithm: Literal["THROUGHPUT_BASED", "NONE"] = Field(
        default="THROUGHPUT_BASED",
        description=(
            "Dataflow autoscaling mode. Default 'THROUGHPUT_BASED'. "
            "Pinned explicitly to prevent drift across runner versions."
        ),
    )
    number_of_worker_harness_threads: int = Field(
        default=8,
        gt=0,
        description=(
            "Per-worker fetcher concurrency. Default 8 (2x n2-standard-4 vCPUs) "
            "to stay within typical EE HV project quota caps while keeping I/O "
            "threads busy during HTTP round-trips."
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

    The envelope holds the runner-agnostic fields (auth, snapshot pin,
    runner selection, rate limit) plus a ``pipeline_kind`` discriminator
    and the kind-specific payload. Today only ``pipeline_kind="pixel"``
    is supported; the payload lives under :attr:`pixel`. A future vector
    pipeline will add a sibling field (e.g. ``vector: VectorPayload``)
    selected by ``pipeline_kind="vector"``.

    The ``ee_expression`` field is opaque; the CLI never interprets it.
    Earth Engine evaluates it per-tile via the High Volume API.

    Back-compat: legacy callers that pass ``tile_grid`` / ``output`` at
    the top level (and equivalent legacy JSON files) are migrated to the
    nested form by :meth:`_migrate_flat_to_nested`. The ``tile_grid``
    and ``output`` attributes remain readable as properties that
    delegate to ``self.pixel`` so internal code paths don't churn.
    """

    pipeline_kind: Literal["pixel"] = Field(
        default="pixel",
        description=(
            "Discriminator selecting which payload shape applies. Only "
            "'pixel' is implemented today; a future vector pipeline will "
            "add 'vector' as a sibling, with its own nested payload."
        ),
    )
    ee_expression: str = Field(description="Serialized EE computation (opaque JSON string)")
    gee_project: str = Field(
        description="GCP project ID with Earth Engine API enabled (used in HV API URL)"
    )
    runner: RunnerConfig = Field(default_factory=RunnerConfig)
    snapshot_time: int | None = Field(
        default=None,
        description=(
            "Unix microseconds at which asset references are pinned. "
            "Microseconds is what EE's version load argument expects."
        ),
    )
    carryover_file: str | None = Field(
        default=None,
        description=(
            "GCS URI or local path of carryover journal staged by retry. "
            "Unioned into _failures.json by the pipeline."
        ),
    )
    pixel: PixelPayload | None = Field(
        default=None,
        description=(
            "Pixel-pipeline payload (tile grid + raster output). Required "
            "when pipeline_kind='pixel', which is currently the only kind."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_flat_to_nested(cls, data: Any) -> Any:
        """Translate legacy flat tile_grid/output into nested pixel payload."""
        if not isinstance(data, dict):
            return data
        if data.get("pixel") is not None:
            return data
        if "tile_grid" not in data and "output" not in data:
            return data

        tg = data.pop("tile_grid", None)
        out = data.pop("output", None)
        data["pixel"] = {"tile_grid": tg, "output": out}
        return data

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> PipelineConfig:
        if self.pipeline_kind == "pixel" and self.pixel is None:
            raise ValueError("pipeline_kind='pixel' requires a 'pixel' payload")
        return self

    @model_validator(mode="after")
    def ee_expression_is_valid_json(self) -> PipelineConfig:
        try:
            json.loads(self.ee_expression)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"ee_expression must be valid JSON: {exc}") from exc
        return self

    @model_validator(mode="after")
    def output_tile_size_divisible(self) -> PipelineConfig:
        if self.pixel is None:
            return self
        out_size = self.pixel.output.output_tile_size_pixels
        if out_size is None:
            return self
        compute_size = self.pixel.tile_grid.tile_size_pixels
        if out_size % compute_size != 0:
            raise ValueError(
                f"output.output_tile_size_pixels ({out_size}) must be a "
                f"multiple of tile_grid.tile_size_pixels ({compute_size})."
            )
        if out_size < compute_size:
            raise ValueError(
                f"output.output_tile_size_pixels ({out_size}) must be >= "
                f"tile_grid.tile_size_pixels ({compute_size})."
            )
        return self

    # --- Back-compat accessors that delegate into the pixel payload. ---

    @property
    def tile_grid(self) -> TileGrid:
        """Pixel payload tile grid."""
        if self.pixel is None:
            raise ValueError("PipelineConfig has no pixel payload")
        return self.pixel.tile_grid

    @property
    def output(self) -> OutputConfig:
        """Pixel payload output config."""
        if self.pixel is None:
            raise ValueError("PipelineConfig has no pixel payload")
        return self.pixel.output

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

    @property
    def effective_output_tile_size_pixels(self) -> int:
        """Output COG edge length: either the configured value or the compute tile size."""
        return self.output.output_tile_size_pixels or self.tile_grid.tile_size_pixels

    @property
    def expected_output_tile_count(self) -> int:
        """Number of output COG files the pipeline will produce.

        Equal to the compute tile count except in two-tier mode, where
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
