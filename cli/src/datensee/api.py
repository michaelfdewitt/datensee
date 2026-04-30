"""Public Python API for DatensEE.

This module exposes the orchestration logic as importable functions,
decoupled from CLI concerns (Rich output, file reading, typer).
The CLI in main.py is a thin wrapper around these functions.
"""

from __future__ import annotations

import json
import os
import tempfile
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


def demo_expression() -> str:
    """Return the serialized EE expression for the built-in NDVI demo."""
    return _load_data("demo_expression.json").strip()


def demo_region() -> dict[str, Any]:
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
    output_bytes: int | None = None


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

_VALID_GEOJSON_TYPES = {"Polygon", "MultiPolygon"}
_GCS_URI_PATTERN = "gs://"


def validate_inputs(
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
    output_tile_size: int | None = None,
    runner: Literal["local", "dataflow"] = "dataflow",
    region_gcp: str = "us-central1",
    temp_location: str | None = None,
    max_qps: int = 100,
    labels: dict[str, str] | None = None,
    jar: Path | str | None = None,
    snapshot_time: int | None = None,
    dry_run: bool = False,
    progress_callback: Callable[[int, int], None] | None = None,
    confirm_callback: Callable[[PipelineConfig], None] | None = None,
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
        tile_size: Compute tile edge size in pixels (sent to the EE HV API).
        output_tile_size: M6 two-tier tiling — output COG edge size in
            pixels. Must be a positive multiple of ``tile_size``. When
            None (the default), one COG is written per compute tile.
            When set to a multiple > tile_size, compute tiles are grouped
            and assembled into larger COGs whose internal block size is
            tile_size. Decouples fetch parallelism from output file count.
        runner: 'local' or 'dataflow'.
        region_gcp: Dataflow region (e.g. 'us-central1').
        temp_location: GCS URI for Dataflow temp files (required for Dataflow).
        max_qps: Max queries per second to the EE HV API.
        labels: Dataflow job labels, forwarded as additionalUserLabels on
            the Flex Template launch. Only applied in 'dataflow' mode.
            Useful for filtering jobs.list responses by caller / tag.
        jar: Path to the pipeline JAR (auto-detected if None).
        snapshot_time: Unix nanos to pin every asset reference in
            ``ee_expression`` to. Defaults to wall-clock now at submit
            time. Override only when you need a deterministic snapshot
            (e.g. reproducing a prior export). Workers see a consistent
            view of mutable assets across the whole job.
        dry_run: If True, validate but don't submit.
        progress_callback: Optional callback(completed, total) for local
            mode progress. Ignored for Dataflow mode.
        confirm_callback: Optional callback(PipelineConfig) invoked after
            the config is built but before submission. Use this to render
            an export summary and gate large jobs behind a prompt — raise
            an exception (e.g. ``typer.Abort``) to abort. Ignored when
            ``dry_run=True``.
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

    # Caller-supplied credentials bypass the ADC bootstrap so we never
    # silently fall back to host ADC when a service is acting as a
    # specific end user.
    if credentials is None:
        ensure_auth()

    geojson_geometry = region

    # Validate
    validation_errors = validate_inputs(ee_expression, geojson_geometry, crs, output, runner)
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

    # Snapshot the *user-supplied* expression for the meta sidecar before
    # we wrap it in clip — `datensee retry` is invoked with the user's
    # original expression and needs to hash to the same value.
    ee_expression_user = ee_expression

    # Pin every asset load in the expression to a single snapshot time.
    # All workers share this T so a mutating ImageCollection can't let
    # tile A see the new version while tile B sees the old one.
    from datensee.pinning import pin_expression

    snapshot_time_nanos = snapshot_time if snapshot_time is not None else time.time_ns()
    ee_expression = pin_expression(ee_expression, snapshot_time_nanos)

    # Clip expression to region
    ee_expression = clip_expression(ee_expression, geojson_geometry)

    # Tile
    tile_grid = decompose_region(
        geojson_geometry=geojson_geometry,
        scale_meters=scale,
        crs=crs,
        tile_size_pixels=tile_size,
        output_tile_size_pixels=output_tile_size,
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
        output=OutputConfig(
            output_path=output,
            output_tile_size_pixels=output_tile_size,
        ),
        runner=runner_config,
        rate_limit=RateLimitConfig(max_qps=max_qps),
        snapshot_time=snapshot_time_nanos,
    )

    # The confirm callback fires for both dry-runs and real submissions so
    # the CLI can render the export summary either way; raising from here
    # aborts before any cloud action.
    if confirm_callback is not None:
        confirm_callback(pipeline_config)

    if dry_run:
        return ExportResult(config=pipeline_config)

    # The local Direct runner needs a JAR on disk; the Dataflow Flex
    # Template path runs the pipeline JAR inside a launcher container, so
    # the user's machine does not need it.
    jar_path: Path | None = None
    if runner == "local":
        if jar is not None:
            from datensee.jar import find_jar

            jar_path = find_jar(Path(jar) if isinstance(jar, str) else jar)
        else:
            jar_path = ensure_jar()

    # Persist the export shape so a later `datensee retry` can verify
    # its args match. Done before submit so the sidecar exists even if
    # the pipeline crashes — a retry against a partially-completed
    # export is exactly the case where the meta is most useful.
    from datensee.meta import build_meta, write_meta

    write_meta(
        output,
        build_meta(
            crs=crs,
            scale_meters=scale,
            tile_size_pixels=tile_size,
            output_tile_size_pixels=output_tile_size,
            gee_project=project,
            ee_expression=ee_expression_user,
            snapshot_time=snapshot_time_nanos,
        ),
        credentials=credentials,
    )

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

    # Post-processing for local mode: count what landed on disk.
    tiles_ok: int | None = None
    tiles_failed: int | None = None

    if runner == "local" and not output.startswith("gs://"):
        output_dir = Path(output)
        tiles_ok = len(list(output_dir.glob("tile_*.tif")))
        tiles_failed = max(0, pipeline_config.expected_output_tile_count - tiles_ok)

    return ExportResult(
        config=pipeline_config,
        job_id=job_id,
        duration_seconds=duration,
        tiles_ok=tiles_ok,
        tiles_failed=tiles_failed,
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
    confirm_callback: Callable[[PipelineConfig], None] | None = None,
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
        confirm_callback: See :func:`export`.

    Returns:
        ExportResult with config and job details.
    """
    return export(
        ee_expression=demo_expression(),
        region=demo_region(),
        project=project,
        output=output,
        scale=30.0,
        crs="EPSG:4326",
        tile_size=512,
        runner="local",
        jar=jar,
        dry_run=dry_run,
        progress_callback=progress_callback,
        confirm_callback=confirm_callback,
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


# ---------------------------------------------------------------------------
# Adaptive retry — quadtree splitter against a failures journal
# ---------------------------------------------------------------------------


class RetryResult(BaseModel):
    """Outcome of a `datensee retry` run.

    ``next_tiles_count`` is the number of TileCoordinates fed back into
    the pipeline (split children + same-bbox retries). ``stats`` is the
    breakdown by action (split / retry_same / depth_cap / terminal /
    unknown_kind / split_disabled). ``carryover_count`` is how many
    original journal entries did not make progress this round.

    ``tiles_failed_this_round`` is the number of fresh failures the
    pipeline emitted on this attempt (i.e. records the pipeline wrote to
    ``_failures.json`` before the retry CLI appended its carryover). Set
    only for local mode; ``None`` when running on Dataflow because the
    pipeline writes the journal asynchronously.
    """

    job_id: str | None = None
    duration_seconds: float = 0.0
    next_tiles_count: int = 0
    carryover_count: int = 0
    stats: dict[str, int] = {}
    tiles_failed_this_round: int | None = None


def retry(
    *,
    output: str,
    journal: Path | str | None = None,
    ee_expression: str | None = None,
    project: str | None = None,
    scale: float | None = None,
    crs: str | None = None,
    tile_size: int | None = None,
    output_tile_size: int | None = None,
    runner: Literal["local", "dataflow"] = "local",
    region_gcp: str = "us-central1",
    temp_location: str | None = None,
    max_qps: int = 100,
    labels: dict[str, str] | None = None,
    jar: Path | str | None = None,
    max_depth: int = 2,
    dry_run: bool = False,
    progress_callback: Callable[[int, int], None] | None = None,
    credentials: Credentials | None = None,
) -> RetryResult:
    """Re-submit failed tiles from a journal, splitting where appropriate.

    Reads ``journal`` (an NDJSON ``_failures.json`` written by a prior
    pipeline run), classifies each entry by ``error_kind``, and for
    split-eligible kinds emits 4 quadtree children — for retry-same kinds,
    re-emits the same bbox. The resulting tile set is written to a
    temporary NDJSON file and submitted to the same pipeline via
    ``tile_grid.tiles_file``.

    Shape arguments default to the values persisted in
    ``{output}/_export_meta.json`` by the original export. With that
    sidecar present, calling ``retry(output=PATH)`` is sufficient —
    everything else is read from disk. Any explicitly-passed arg must
    match the persisted value or :class:`meta.ExportMetaMismatch` is
    raised, since a mismatch would key new COGs to a different output
    grid than the existing ones.

    Args:
        output: Output path of the original export. Doubles as the
            anchor for the failures journal and the meta sidecar.
        journal: Path to the failures journal (NDJSON of FailedTileRecord).
            Defaults to ``{output}/_failures.json``.
        ee_expression: Same EE expression as the original export. When
            None, read from ``_export_meta.json``.
        project: Same GCP project. When None, read from meta.
        scale, crs, tile_size, output_tile_size: Must match the
            original export so split children align with the output
            grid. When None, read from meta. ``output_tile_size``
            staying ``None`` means the original was one-COG-per-compute
            (non-M6); split actions are then demoted to
            ``split_disabled`` carryover.
        max_depth: Max quadtree depth. Records already at this depth
            are not split — they remain in the next round's failures.
            Default 2 (one root → 16 sub-tiles max).
        dry_run: If True, plan the retry but don't submit.
        Other args are forwarded to ``submit_job`` as in :func:`export`.

    Returns:
        RetryResult with the submitted job id (if any) plus stats.

    Raises:
        FileNotFoundError: If no meta sidecar exists and a required
            shape arg was not supplied.
        meta.ExportMetaMismatch: If a passed arg disagrees with meta.
    """
    import logging

    from datensee.meta import (
        EXPORT_META_FILENAME,
        read_meta,
        verify_retry_compatibility,
    )
    from datensee.notebook import ensure_auth, ensure_jar
    from datensee.retry import plan_retry, read_journal, write_tiles_file
    from datensee.submit import submit_job

    if credentials is None:
        ensure_auth()

    # Resolve shape args against the persisted meta. The meta is the
    # canonical source of "what the original export was"; any explicit
    # arg the caller passes must agree with it. A missing sidecar
    # (legacy export) means we can't verify — we fall back to whatever
    # the caller supplied, and only the function-default values for
    # anything they left blank.
    persisted_meta = read_meta(output, credentials=credentials)
    if persisted_meta is not None:
        # Fill in any None args from meta.
        if ee_expression is None:
            ee_expression = persisted_meta.ee_expression
        if project is None:
            project = persisted_meta.gee_project
        if scale is None:
            scale = persisted_meta.scale_meters
        if crs is None:
            crs = persisted_meta.crs
        if tile_size is None:
            tile_size = persisted_meta.tile_size_pixels
        if output_tile_size is None:
            output_tile_size = persisted_meta.output_tile_size_pixels
        # Now verify everything (caller-passed values too) matches.
        verify_retry_compatibility(
            persisted_meta,
            crs=crs,
            scale_meters=scale,
            tile_size_pixels=tile_size,
            output_tile_size_pixels=output_tile_size,
            gee_project=project,
            ee_expression=ee_expression,
        )
    else:
        # Legacy export — no meta to fall back on. Apply documented
        # defaults for the fields that have them; require the rest.
        if scale is None:
            scale = 30.0
        if crs is None:
            crs = "EPSG:4326"
        if tile_size is None:
            tile_size = 512
        missing = [
            name
            for name, value in (
                ("ee_expression", ee_expression),
                ("project", project),
            )
            if value is None
        ]
        if missing:
            raise FileNotFoundError(
                f"{EXPORT_META_FILENAME} not found in {output!r} and "
                f"required arg(s) not provided: {', '.join(missing)}. "
                "Either pass them explicitly or run the original export "
                "with a datensee version that writes the meta sidecar."
            )
        logging.getLogger(__name__).warning(
            "datensee retry: %s not found in %s — skipping shape "
            "verification. Be sure your retry args match the original "
            "export, especially output_tile_size.",
            EXPORT_META_FILENAME,
            output,
        )

    # Default the journal path to the canonical location under output.
    if journal is None:
        if output.startswith("gs://"):
            raise ValueError(
                "datensee retry against a GCS output requires --journal "
                "to be set explicitly (we don't auto-locate "
                "{output}/_failures.json on GCS)."
            )
        journal = Path(output) / "_failures.json"

    journal_path = Path(journal) if isinstance(journal, str) else journal
    records = read_journal(journal_path)

    # Splitting requires M6 two-tier output: split children inherit
    # (row, col) from their parent, and the non-M6 writer keys output
    # filenames on (row, col) alone — four successful split children
    # would all write to the same `tile_rNNNN_cNNNN.tif`. Refuse to
    # split when the retry isn't running against a two-tier export.
    allow_split = output_tile_size is not None and output_tile_size > tile_size
    plan = plan_retry(records, max_depth=max_depth, allow_split=allow_split)

    if not plan.next_tiles:
        return RetryResult(
            next_tiles_count=0,
            carryover_count=len(plan.carryover),
            stats=plan.stats,
        )

    # Stage the next-round tiles file. We anchor it under the output
    # directory so a rerun is reproducible from the journal alone.
    if output.startswith("gs://"):
        # GCS: the tiles_file path can be a GCS URI; the Java side reads
        # via TextIO which supports gs://. Stage it as a sibling object.
        tiles_file_path = output.rstrip("/") + "/_retry_tiles.json"
        # Write locally first, then upload.
        local_staging = Path(tempfile.mkdtemp()) / "_retry_tiles.json"
        write_tiles_file(plan.next_tiles, local_staging)
        from google.cloud import storage as _gcs

        client = _gcs.Client(credentials=credentials, project=project)
        gs_uri = tiles_file_path[len("gs://"):]
        bucket_name, _, blob_path = gs_uri.partition("/")
        bucket = client.bucket(bucket_name)
        bucket.blob(blob_path).upload_from_filename(str(local_staging))
    else:
        out_dir = Path(output)
        out_dir.mkdir(parents=True, exist_ok=True)
        local_path = out_dir / "_retry_tiles.json"
        write_tiles_file(plan.next_tiles, local_path)
        tiles_file_path = str(local_path)

    # Build pipeline config with tiles_file (no inline tiles).
    if runner == "dataflow":
        runner_config = RunnerConfig(
            mode="dataflow",
            dataflow=DataflowRunnerConfig(
                project=project,
                region=region_gcp,
                temp_location=temp_location or (output.rstrip("/") + "/_tmp"),
                staging_location=(temp_location or output.rstrip("/") + "/_tmp").rstrip("/")
                + "/staging",
                labels=labels,
            ),
        )
    else:
        runner_config = RunnerConfig(mode="local")

    # Pin retry children to the same snapshot as the original export.
    # If the parent COGs were fetched at T, the new children must also
    # see T or we'd splice newer EE data into a partly-stale output COG.
    # Legacy meta (snapshot_time=None) means the original export was
    # unpinned; we fall back to fresh T and warn — no worse than before
    # for the parents, slightly better for the children.
    from datensee.pinning import pin_expression

    if persisted_meta is not None and persisted_meta.snapshot_time is not None:
        snapshot_time_nanos = persisted_meta.snapshot_time
    else:
        snapshot_time_nanos = time.time_ns()
        logging.getLogger(__name__).warning(
            "datensee retry: no snapshot_time in meta — original export was "
            "unpinned. Retry children will pin to %d (now). The output COGs "
            "may end up with sub-tiles fetched at different snapshots.",
            snapshot_time_nanos,
        )
    ee_expression = pin_expression(ee_expression, snapshot_time_nanos)

    pipeline_config = PipelineConfig(
        ee_expression=ee_expression,
        gee_project=project,
        tile_grid=TileGrid(
            crs=crs,
            scale_meters=scale,
            tile_size_pixels=tile_size,
            tiles_file=tiles_file_path,
        ),
        output=OutputConfig(
            output_path=output,
            output_tile_size_pixels=output_tile_size,
        ),
        runner=runner_config,
        rate_limit=RateLimitConfig(max_qps=max_qps),
        snapshot_time=snapshot_time_nanos,
    )

    if dry_run:
        return RetryResult(
            next_tiles_count=len(plan.next_tiles),
            carryover_count=len(plan.carryover),
            stats=plan.stats,
        )

    jar_path: Path | None = None
    if runner == "local":
        if jar is not None:
            from datensee.jar import find_jar

            jar_path = find_jar(Path(jar) if isinstance(jar, str) else jar)
        else:
            jar_path = ensure_jar()

    t0 = time.monotonic()
    job_id = submit_job(
        pipeline_config,
        jar_path=jar_path,
        dry_run=False,
        progress_callback=progress_callback,
        credentials=credentials,
    )
    duration = time.monotonic() - t0

    # Merge carryover into _failures.json so the journal stays the
    # canonical view of "what's still stuck." The pipeline writes its
    # own _failures.json with this round's new failures; we append
    # records that didn't make progress this round (terminal kinds plus
    # depth-capped split-eligible records) so they're not silently lost
    # between rounds.
    #
    # Local mode only — by the time we return from submit_job, the
    # pipeline has finished and the file is on disk. Dataflow mode is
    # async: the pipeline writes _failures.json after submit_job returns,
    # so we'd be appending to a file that doesn't exist yet (or worse,
    # racing with the pipeline writer). Tracked as a TODO; for now we
    # log + skip in Dataflow mode.
    # Snapshot the pipeline's fresh failures *before* appending carryover,
    # so `tiles_failed_this_round` reflects what the pipeline produced
    # on this attempt and not records we recycled from a prior round.
    tiles_failed_this_round: int | None = None
    if runner == "local" and not output.startswith("gs://"):
        failures_path = Path(output) / "_failures.json"
        if failures_path.exists():
            tiles_failed_this_round = sum(
                1 for line in failures_path.read_text().splitlines() if line.strip()
            )
        else:
            tiles_failed_this_round = 0

    if plan.carryover:
        if runner == "local" and not output.startswith("gs://"):
            failures_path = Path(output) / "_failures.json"
            with failures_path.open("a", encoding="utf-8") as f:
                for record in plan.carryover:
                    f.write(json.dumps(record))
                    f.write("\n")
        else:
            import logging

            logging.getLogger(__name__).warning(
                "datensee retry: %d carryover records (terminal/depth-cap) not "
                "merged into _failures.json — Dataflow / GCS merge isn't "
                "wired up yet. Re-feeding the original journal will surface "
                "them again next round.",
                len(plan.carryover),
            )

    return RetryResult(
        job_id=job_id,
        duration_seconds=duration,
        next_tiles_count=len(plan.next_tiles),
        carryover_count=len(plan.carryover),
        tiles_failed_this_round=tiles_failed_this_round,
        stats=plan.stats,
    )
