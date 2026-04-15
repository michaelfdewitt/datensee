"""Public Python API for DatensEE.

This module exposes the orchestration logic as importable functions,
decoupled from CLI concerns (Rich output, file reading, typer).
The CLI in main.py is a thin wrapper around these functions.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

import pyproj
from pydantic import BaseModel

from datensee.config import (
    DataflowRunnerConfig,
    OutputConfig,
    PipelineConfig,
    RateLimitConfig,
    RunnerConfig,
    TileGrid,
)
from datensee.expression import clip_expression
from datensee.tiling import decompose_region

# ---------------------------------------------------------------------------
# Demo data — loaded from JSON files in data/
# ---------------------------------------------------------------------------


def _load_data(name: str) -> str:
    """Load a bundled data file as a string."""
    return (Path(__file__).parent / "data" / name).read_text()


def _demo_expression() -> str:
    """Return the serialized EE expression for the built-in NDVI demo."""
    return _load_data("demo_expression.json").strip()


def _demo_region() -> dict[str, Any]:
    """Return the GeoJSON region for the built-in NDVI demo."""
    return json.loads(_load_data("demo_region.json"))


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class ExportResult(BaseModel):
    """Result of an export() or demo() call."""

    config: PipelineConfig
    job_id: str | None = None
    duration_seconds: float | None = None
    tiles_ok: int | None = None
    tiles_failed: int | None = None
    vrt_path: str | None = None
    output_bytes: int | None = None


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

_VALID_GEOJSON_TYPES = {"Polygon", "MultiPolygon"}
_GCS_URI_PATTERN = "gs://"


def _validate_inputs(
    ee_expression: str,
    geojson_geometry: dict[str, Any],
    crs: str,
    output: str,
    runner: str,
) -> list[str]:
    """Validate export inputs before job submission. Returns list of errors."""
    errors: list[str] = []

    # 1. ee_expression must be valid JSON
    try:
        json.loads(ee_expression)
    except (json.JSONDecodeError, TypeError) as exc:
        errors.append(
            f"Expression file is not valid JSON: {exc}. "
            "Provide a file containing a serialized EE computation "
            "(output of ee.serializer.encode())."
        )

    # 2. Region must be a Polygon or MultiPolygon (or Feature wrapping one)
    geom_type = geojson_geometry.get("type")
    if geom_type == "Feature":
        geom_type = (geojson_geometry.get("geometry") or {}).get("type")
    if geom_type == "FeatureCollection":
        errors.append(
            "Region GeoJSON type is 'FeatureCollection', but a single "
            "Polygon or MultiPolygon is required. Extract one feature first."
        )
    elif geom_type not in _VALID_GEOJSON_TYPES:
        errors.append(
            f"Region GeoJSON type is '{geom_type}', but must be one of "
            f"{sorted(_VALID_GEOJSON_TYPES)}. Points and lines cannot define "
            f"an export region."
        )

    # 3. CRS must be parseable by pyproj
    try:
        pyproj.CRS.from_user_input(crs)
    except pyproj.exceptions.CRSError as exc:
        errors.append(
            f"CRS '{crs}' is not recognized: {exc}. "
            "Use an EPSG code (e.g. 'EPSG:4326') or a valid proj string."
        )

    # 4. Output path validation
    if runner == "local" and not output.startswith(_GCS_URI_PATTERN):
        output_path = Path(output)
        parent = output_path if output_path.is_dir() else output_path.parent
        if parent.exists() and not os.access(parent, os.W_OK):
            errors.append(
                f"Output directory '{parent}' is not writable. "
                "Check permissions or choose a different path."
            )
    elif runner == "dataflow" and not output.startswith(_GCS_URI_PATTERN):
        errors.append(
            f"Dataflow mode requires a GCS output path (gs://…), "
            f"but got '{output}'. Provide a GCS URI."
        )

    return errors


# ---------------------------------------------------------------------------
# TileGrid helper
# ---------------------------------------------------------------------------


def tile(
    region: dict[str, Any],
    scale: float = 30.0,
    crs: str = "EPSG:4326",
    tile_size: int = 512,
) -> TileGrid:
    """Decompose a GeoJSON region into a tile grid.

    Args:
        region: GeoJSON Polygon or MultiPolygon geometry dict (WGS84).
        scale: Pixel size in meters.
        crs: Target CRS (EPSG code or proj string).
        tile_size: Tile edge size in pixels.

    Returns:
        TileGrid with computed tile coordinates.
    """
    return decompose_region(
        geojson_geometry=region,
        scale_meters=scale,
        crs=crs,
        tile_size_pixels=tile_size,
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export(
    ee_expression: str,
    region: dict[str, Any],
    project: str,
    output: str,
    *,
    scale: float = 30.0,
    crs: str = "EPSG:4326",
    tile_size: int = 512,
    runner: Literal["local", "dataflow"] = "dataflow",
    region_gcp: str = "us-central1",
    temp_location: str | None = None,
    max_qps: int = 100,
    labels: dict[str, str] | None = None,
    jar: Path | str | None = None,
    dry_run: bool = False,
    progress_callback: Callable[[int, int], None] | None = None,
    credentials: Credentials | None = None,
) -> ExportResult:
    """Submit an Earth Engine export job.

    This is the programmatic equivalent of `datensee export`. It validates
    inputs, tiles the region, builds the pipeline config, and submits the job.

    For Dataflow mode, returns immediately after submission with the job_id.
    For local mode, blocks until the pipeline completes.

    Args:
        ee_expression: Serialized EE computation (JSON string from
            ee.serializer.encode()).
        region: GeoJSON Polygon or MultiPolygon geometry dict (WGS84).
        project: GCP project ID with Earth Engine API enabled.
        output: Output path — GCS URI (gs://…) for Dataflow, or local dir.
        scale: Pixel size in meters.
        crs: Target CRS (EPSG code or proj string).
        tile_size: Tile edge size in pixels.
        runner: 'local' or 'dataflow'.
        region_gcp: Dataflow region (e.g. 'us-central1').
        temp_location: GCS URI for Dataflow temp files (required for Dataflow).
        max_qps: Max queries per second to the EE HV API.
        labels: Dataflow job labels, forwarded to the runner as --labels=JSON.
            Only applied in 'dataflow' mode. Useful for filtering jobs.list
            responses downstream (e.g. {"foundree": "1"}).
        jar: Path to the pipeline JAR (auto-detected if None).
        dry_run: If True, validate but don't submit.
        progress_callback: Optional callback(completed, total) for local
            mode progress. Ignored for Dataflow mode.
        credentials: Optional caller-supplied Google credentials. When set,
            DatensEE skips its normal ADC bootstrap (`ensure_auth()`) and
            uses these credentials for every Google API call — GCS uploads
            on the driver and Dataflow job submission in the Java worker
            (threaded through a short-lived env var). Use this when the
            caller is a service that holds an end-user's OAuth token and
            must not fall back to its own ambient ADC.

    Returns:
        ExportResult with config and job details.

    Raises:
        ValueError: If inputs fail validation.
        FileNotFoundError: If the pipeline JAR cannot be found.
    """
    from datensee.notebook import ensure_auth, ensure_jar

    # Caller-supplied credentials bypass the ADC bootstrap entirely so
    # we never silently fall back to host ADC (see security note in the
    # FoundrEE bridge — the driver must act as the end user, not as the
    # host service account).
    if credentials is None:
        ensure_auth()

    geojson_geometry = region.copy()

    # Validate
    validation_errors = _validate_inputs(ee_expression, geojson_geometry, crs, output, runner)
    if runner == "dataflow" and not temp_location:
        validation_errors.append(
            "--temp-location is required for Dataflow mode. "
            "Provide a GCS URI (gs://…) for Dataflow temp files."
        )
    if validation_errors:
        raise ValueError("\n".join(validation_errors))

    # Unwrap Feature → geometry
    if geojson_geometry.get("type") == "Feature":
        geojson_geometry = geojson_geometry["geometry"]

    # Clip expression to region
    ee_expression = clip_expression(ee_expression, geojson_geometry)

    # Tile
    tile_grid = decompose_region(
        geojson_geometry=geojson_geometry,
        scale_meters=scale,
        crs=crs,
        tile_size_pixels=tile_size,
    )

    # Build config
    if runner == "dataflow":
        runner_config = RunnerConfig(
            mode="dataflow",
            dataflow=DataflowRunnerConfig(
                project=project,
                region=region_gcp,
                temp_location=temp_location,
                staging_location=temp_location.rstrip("/") + "/staging",
                labels=labels,
            ),
        )
    else:
        runner_config = RunnerConfig(mode="local")

    pipeline_config = PipelineConfig(
        ee_expression=ee_expression,
        gee_project=project,
        tile_grid=tile_grid,
        output=OutputConfig(output_path=output),
        runner=runner_config,
        rate_limit=RateLimitConfig(max_qps=max_qps),
    )

    if dry_run:
        return ExportResult(config=pipeline_config)

    # Resolve JAR
    if jar is not None:
        from datensee.jar import find_jar

        jar_path = find_jar(Path(jar) if isinstance(jar, str) else jar)
    else:
        jar_path = ensure_jar()

    # Submit
    from datensee.submit import submit_job

    t0 = time.monotonic()
    job_id = submit_job(
        pipeline_config,
        jar_path=jar_path,
        dry_run=False,
        progress_callback=progress_callback,
        credentials=credentials,
    )
    duration = time.monotonic() - t0

    # Post-processing for local mode
    tiles_ok: int | None = None
    tiles_failed: int | None = None
    vrt_path: str | None = None

    if runner == "local" and not output.startswith("gs://"):
        from datensee.assemble import write_vrt

        output_dir = Path(output)
        vrt = write_vrt(pipeline_config, output_dir)
        vrt_path = str(vrt)
        tiles_ok = len(list(output_dir.glob("tile_*.tif")))
        tiles_failed = max(0, pipeline_config.tile_count - tiles_ok)

    return ExportResult(
        config=pipeline_config,
        job_id=job_id,
        duration_seconds=duration,
        tiles_ok=tiles_ok,
        tiles_failed=tiles_failed,
        vrt_path=vrt_path,
    )


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


def demo(
    project: str,
    output: str = "./datensee-output",
    *,
    jar: Path | str | None = None,
    dry_run: bool = False,
    progress_callback: Callable[[int, int], None] | None = None,
) -> ExportResult:
    """Run the built-in NDVI demo over SF Bay Area.

    Equivalent to `datensee demo --project <project>`. Uses the local
    runner with a hardcoded Landsat 9 NDVI expression.

    Args:
        project: GCP project ID with Earth Engine API enabled.
        output: Local directory for output tiles + VRT.
        jar: Path to the pipeline JAR (auto-detected if None).
        dry_run: If True, validate but don't submit.
        progress_callback: Optional callback(completed, total) for progress.

    Returns:
        ExportResult with config and job details.
    """
    return export(
        ee_expression=_demo_expression(),
        region=_demo_region(),
        project=project,
        output=output,
        scale=30.0,
        crs="EPSG:4326",
        tile_size=512,
        runner="local",
        jar=jar,
        dry_run=dry_run,
        progress_callback=progress_callback,
    )


# ---------------------------------------------------------------------------
# Poll
# ---------------------------------------------------------------------------


def poll(
    job_id: str,
    project: str,
    region: str = "us-central1",
    *,
    callback: Callable[[Any], None] | None = None,
    poll_interval: int = 15,
) -> Any:
    """Poll a Dataflow job until it reaches a terminal state.

    Args:
        job_id: Dataflow job ID.
        project: GCP project ID.
        region: Dataflow region (e.g. 'us-central1').
        callback: Optional callback(JobInfo) called on each poll tick.
            If None, uses Rich Live display (CLI mode).
        poll_interval: Seconds between polls.

    Returns:
        Final JobState.
    """
    from datensee.notebook import ensure_auth

    ensure_auth()

    from datensee.auth import get_access_token
    from datensee.status import poll_job

    access_token = get_access_token()
    return poll_job(
        job_id=job_id,
        project=project,
        region=region,
        access_token=access_token,
        poll_interval_seconds=poll_interval,
        status_callback=callback,
    )
