"""Pydantic models for pipeline configuration.

This module defines the runner-agnostic envelope of the contract between
the Python CLI and the Java Beam pipeline — submission/auth/snapshot
fields plus the discriminated payload that describes the actual work.

The serialized form matches ``contract/pipeline-config.schema.json``.

Today the only payload shape is :class:`PixelPayload` (raster tiling +
COG output), defined in :mod:`datensee.pixel.config` and selected by
``pipeline_kind="pixel"``. A future vector pipeline will plug a sibling
payload onto the same envelope without touching the pixel side.

Legacy flat input (``tile_grid`` / ``output`` at the top level) is
migrated to the nested form transparently by a ``mode="before"`` model
validator, so existing Python callers and on-disk
``_pipeline-config.json`` files written before the discriminator
landed continue to work.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# Pixel-shaped models live in datensee.pixel.config. Re-exported here so
# `from datensee.config import PixelGrid, TileGrid, ...` keeps working
# until callers migrate to the explicit pixel-subpackage import.
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

    The envelope holds the runner-agnostic fields (auth, snapshot pin,
    runner selection, rate limit) plus a ``pipeline_kind`` discriminator
    and the kind-specific payload. Today only ``pipeline_kind="pixel"``
    is supported; the payload lives under :attr:`pixel`. A future vector
    pipeline will add a sibling field (e.g. ``vector: VectorPayload``)
    selected by ``pipeline_kind="vector"``.

    The ``ee_expression`` field is opaque — the CLI never interprets it.
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
            "Unix microseconds at which the EE expression's asset "
            "references were pinned (snapshot consistency across "
            "parallel tile fetches). Set by api.export(); read by "
            "api.retry() to pin split children to the same snapshot "
            "as their parents. Microseconds is what EE's `version` "
            "load argument actually expects — nanoseconds lands in "
            "an INTERNAL-crash range."
        ),
    )
    carryover_file: str | None = Field(
        default=None,
        description=(
            "GCS URI or local path of a carryover journal staged by "
            "`datensee retry`: the previous round's no-progress records "
            "(terminal kinds, depth-capped splits), already stamped with "
            "their journal_reason. The pipeline unions these lines with "
            "this run's fresh failures when writing _failures.json, so "
            "the journal stays the complete view of stuck tiles on every "
            "runner — no Python post-step racing the async writer."
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
        """Translate legacy ``tile_grid`` / ``output`` at the top level into
        the nested ``pixel`` payload.

        Runs before field validation, so callers that pass kwargs in the
        flat shape (a lot of test code, plus
        ``_pipeline-config.json`` written by datensee before the
        discriminator landed) still construct a valid model. If ``pixel``
        is already present, the input is assumed nested and passes
        through untouched.
        """
        if not isinstance(data, dict):
            return data
        if data.get("pixel") is not None:
            return data
        if "tile_grid" not in data and "output" not in data:
            return data

        tg = data.pop("tile_grid", None)
        out = data.pop("output", None)
        # `output` may arrive as a dict (JSON) or an OutputConfig instance
        # (kwarg construction); both are fine — PixelPayload's Pydantic
        # validators normalize them downstream.
        data["pixel"] = {"tile_grid": tg, "output": out}
        return data

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> PipelineConfig:
        """Enforce that the declared kind has its matching payload set.

        Today the only kind is ``"pixel"`` and the only payload field is
        ``pixel``; when a vector payload lands this validator extends to
        cover that case symmetrically. Without this check a config could
        declare ``pipeline_kind="pixel"`` with no payload, and the Java
        side would NPE rather than failing loudly here.
        """
        if self.pipeline_kind == "pixel" and self.pixel is None:
            raise ValueError("pipeline_kind='pixel' requires a 'pixel' payload")
        return self

    @model_validator(mode="after")
    def ee_expression_is_valid_json(self) -> PipelineConfig:
        try:
            json.loads(self.ee_expression)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"ee_expression must be a valid JSON string: {exc}") from exc
        return self

    @model_validator(mode="after")
    def output_tile_size_must_be_multiple_of_compute_tile_size(self) -> PipelineConfig:
        if self.pixel is None:
            return self
        out_size = self.pixel.output.output_tile_size_pixels
        if out_size is None:
            return self
        compute_size = self.pixel.tile_grid.tile_size_pixels
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

    # --- Back-compat accessors that delegate into the pixel payload. ---
    # Internal callers historically did `config.tile_grid` /
    # `config.output`; keeping these as properties means the Phase 2
    # restructure doesn't fan out into every consumer.

    @property
    def tile_grid(self) -> TileGrid:
        """Pixel payload's tile grid — see :class:`PixelPayload`."""
        if self.pixel is None:
            raise ValueError("PipelineConfig has no pixel payload")
        return self.pixel.tile_grid

    @property
    def output(self) -> OutputConfig:
        """Pixel payload's output config — see :class:`PixelPayload`."""
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
