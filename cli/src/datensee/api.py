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
    RunnerConfig,
    TileGrid,
)
from datensee.pixel.tiling import decompose_region

# ---------------------------------------------------------------------------
# Demo data — loaded from JSON files in data/
# ---------------------------------------------------------------------------


def _load_data(name: str) -> str:
    """Load a bundled data file as a string."""
    return (Path(__file__).parent / "data" / name).read_text()


def _now_micros() -> int:
    """Wall-clock now as a Unix microsecond timestamp.

    Used to stamp ``snapshot_time`` on every export. Microseconds —
    not nanoseconds — because that's the unit EE's ``version`` load
    argument actually wants; see :mod:`datensee.pinning`.
    """
    return time.time_ns() // 1_000


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
# Input normalization — accept live ee objects without depending on
# earthengine-api. Everything is duck-typed on the object's module and
# methods: when the caller hands us an ee.Image we serialize it with THEIR
# installed client; when they hand us a string we pass it through. This is
# what makes the Export.image.toCloudStorage → datensee.export swap a
# one-line change for Python EE users.
# ---------------------------------------------------------------------------


def _is_ee_object(obj: Any) -> bool:
    return (type(obj).__module__ or "").split(".")[0] == "ee"


# EE function names whose return type is ImageCollection, not Image. Not
# exhaustive — it catches the most common mistake: forgetting to reduce a
# collection before exporting, which would otherwise dead-letter every tile.
_COLLECTION_RETURNING_FUNCTIONS: frozenset[str] = frozenset(
    {
        "ImageCollection.load",
        "Collection.filter",
        "Collection.map",
        "Collection.sort",
        "Collection.limit",
        "Collection.distinct",
        "Collection.flatten",
        "Collection.merge",
        "Collection.filterBounds",
        "Collection.filterDate",
        "Collection.filterMetadata",
    }
)

_REDUCE_HINT = (
    "Reduce the collection to a single image first (e.g. .median(), "
    ".mosaic(), .first()) and export that."
)


def _reject_collection_expression(expression: str) -> None:
    """Fail fast when the expression's result node is an ImageCollection."""
    try:
        tree = json.loads(expression)
    except (json.JSONDecodeError, TypeError):
        return  # validate_inputs reports malformed JSON with a better message
    values = tree.get("values", {}) if isinstance(tree, dict) else {}
    node = values.get(tree.get("result")) if isinstance(tree, dict) else None
    invocation = (node or {}).get("functionInvocationValue") or {}
    name = invocation.get("functionName")
    if name in _COLLECTION_RETURNING_FUNCTIONS:
        raise ValueError(
            f"The expression's result is an ImageCollection (via {name!r}), "
            f"but computePixels requires an Image. {_REDUCE_HINT}"
        )


def _normalize_expression(expression: Any) -> str:
    """Return the serialized cloud-API expression string for ``expression``.

    Accepts a pre-serialized JSON string, or a live ``ee.Image`` (any
    ``ee`` computed object with ``serialize()``), serialized with the
    caller's own earthengine-api client — datensee never imports ``ee``.
    """
    if isinstance(expression, str):
        return expression
    if _is_ee_object(expression) and hasattr(expression, "serialize"):
        if type(expression).__name__ == "ImageCollection":
            raise ValueError(f"Cannot export an ee.ImageCollection directly. {_REDUCE_HINT}")
        serialized = expression.serialize()
        try:
            tree = json.loads(serialized)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(
                "serialize() did not produce cloud-API JSON — your "
                "earthengine-api client is emitting the legacy serialization "
                "format. Upgrade earthengine-api (>= 0.1.300 serializes in "
                "cloud-API form by default)."
            ) from exc
        if not (isinstance(tree, dict) and "result" in tree and "values" in tree):
            raise ValueError(
                "serialize() produced JSON without the cloud-API "
                "{'result', 'values'} shape. Upgrade earthengine-api, or pass "
                "json.dumps(ee.serializer.encode(image, for_cloud_api=True))."
            )
        return serialized
    raise TypeError(
        f"ee_expression must be a serialized expression string or an ee.Image; "
        f"got {type(expression).__name__}. In Python: pass the ee.Image object "
        "directly. From a serialized file: pass its contents as a string."
    )


def _normalize_region(region: Any) -> dict[str, Any]:
    """Return a GeoJSON geometry dict for ``region``.

    Accepts a GeoJSON dict, anything implementing ``__geo_interface__``
    (shapely geometries, Fiona features, ...), or a live ``ee.Geometry`` /
    ``ee.Feature`` / ``ee.FeatureCollection``. Client-side ee geometries
    resolve without a network call via ``toGeoJSON()``; server-computed
    ones fall back to ``getInfo()``.
    """
    if isinstance(region, dict):
        return region
    if _is_ee_object(region):
        geom = region
        if type(region).__name__ in {"Feature", "FeatureCollection", "Image"} and hasattr(
            region, "geometry"
        ):
            geom = region.geometry()
        if hasattr(geom, "toGeoJSON"):
            try:
                return geom.toGeoJSON()
            except Exception:  # noqa: BLE001 — server-computed geometry; fall through
                pass
        return geom.getInfo()
    if hasattr(region, "__geo_interface__"):
        return dict(region.__geo_interface__)
    raise TypeError(
        f"region must be a GeoJSON dict, a shapely geometry, or an "
        f"ee.Geometry/Feature/FeatureCollection; got {type(region).__name__}."
    )


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
    region: Any,
    scale: float = 30.0,
    crs: str = "EPSG:4326",
    tile_size: int = 512,
) -> TileGrid:
    """Decompose a region into a tile grid.

    Args:
        region: GeoJSON Polygon/MultiPolygon dict (WGS84), a shapely
            geometry, or an ee.Geometry/Feature/FeatureCollection.
        scale: Pixel size in meters.
        crs: Target CRS (EPSG code or proj string).
        tile_size: Tile edge size in pixels.

    Returns:
        TileGrid with computed tile coordinates.
    """
    return decompose_region(
        geojson_geometry=_normalize_region(region),
        scale_meters=scale,
        crs=crs,
        tile_size_pixels=tile_size,
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export(
    ee_expression: Any,
    region: Any,
    project: str,
    output: str,
    *,
    scale: float = 30.0,
    crs: str = "EPSG:4326",
    tile_size: int = 512,
    output_tile_size: int | None = None,
    nodata: float | None = None,
    band_count: int = 1,
    data_type: str = "float32",
    runner: Literal["local", "dataflow"] = "dataflow",
    region_gcp: str = "us-central1",
    temp_location: str | None = None,
    labels: dict[str, str] | None = None,
    machine_type: str | None = None,
    num_workers: int | None = None,
    max_workers: int | None = None,
    autoscaling_algorithm: Literal["THROUGHPUT_BASED", "NONE"] | None = None,
    number_of_worker_harness_threads: int | None = None,
    jar: Path | str | None = None,
    snapshot_time: int | None = None,
    dry_run: bool = False,
    progress_callback: Callable[[int, int], None] | None = None,
    confirm_callback: Callable[[PipelineConfig], None] | None = None,
    credentials: Credentials | None = None,
) -> ExportResult:
    """Submit an Earth Engine export job.

    This is the programmatic equivalent of `datensee export`, and the
    drop-in replacement for ``Export.image.toCloudStorage``::

        # before                                  # after
        Export.image.toCloudStorage(              datensee.export(
            image, region=geom, scale=30,             image, region=geom, scale=30,
            bucket='b', fileNamePrefix='x')           project='p', output='gs://b/x')

    It validates inputs, tiles the region, builds the pipeline config, and
    submits the job. For Dataflow mode, returns immediately after
    submission with the job_id. For local mode, blocks until the pipeline
    completes.

    Args:
        ee_expression: A live ``ee.Image`` (serialized with your own
            earthengine-api client — datensee never imports ``ee``), or a
            pre-serialized cloud-API expression JSON string.
        region: GeoJSON Polygon or MultiPolygon dict (WGS84), a shapely
            geometry, or an ee.Geometry/Feature/FeatureCollection.
        project: GCP project ID with Earth Engine API enabled.
        output: Output path — GCS URI (gs://…) for Dataflow, or local dir.
        scale: Pixel size in meters.
        crs: Target CRS (EPSG code or proj string).
        tile_size: Compute tile edge size in pixels (sent to the EE HV API).
        output_tile_size: two-tier tiling — output COG edge size in
            pixels. Must be a positive multiple of ``tile_size``. When
            None (the default), one COG is written per compute tile.
            When set to a multiple > tile_size, compute tiles are grouped
            and assembled into larger COGs whose internal block size is
            tile_size. Decouples fetch parallelism from output file count.
        nodata: Optional nodata value stamped on every output COG as the
            GDAL_NODATA tag. EE returns masked pixels as 0 with no mask
            channel — unmask(sentinel) the expression and pass the
            sentinel here so GIS tools can tell nodata from real zeros.
        band_count: Bands your expression produces (default 1). Informational
            for the pipeline (EE responses are self-describing) but recorded
            in the config and meta sidecar; `datensee validate` checks the
            output against it.
        data_type: Pixel dtype your expression produces (default 'float32';
            e.g. 'int16' for SRTM). Same role as band_count.
        runner: 'local' or 'dataflow'.
        region_gcp: Dataflow region (e.g. 'us-central1').
        temp_location: GCS URI for Dataflow temp files (required for Dataflow).
        labels: Dataflow job labels, forwarded as additionalUserLabels on
            the Flex Template launch. Only applied in 'dataflow' mode.
            Useful for filtering jobs.list responses by caller / tag.
        jar: Path to the pipeline JAR (auto-detected if None).
        snapshot_time: Unix microseconds to pin every asset reference
            in ``ee_expression`` to. Defaults to wall-clock now at
            submit time. Override only when you need a deterministic
            snapshot (e.g. reproducing a prior export). Workers see a
            consistent view of mutable assets across the whole job.
        dry_run: If True, validate but don't submit. The export
            summary callback still fires; nothing is written or fetched.
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
    # specific end user. Dry runs never talk to Google APIs, so they
    # skip the bootstrap entirely (services validate configs without
    # any ambient credentials).
    if credentials is None and not dry_run:
        ensure_auth()

    ee_expression = _normalize_expression(ee_expression)
    geojson_geometry = _normalize_region(region)

    # Validate
    validation_errors = validate_inputs(ee_expression, geojson_geometry, crs, output, runner)
    _reject_collection_expression(ee_expression)
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

    snapshot_time_micros = snapshot_time if snapshot_time is not None else _now_micros()
    ee_expression = pin_expression(ee_expression, snapshot_time_micros)

    # Note: we deliberately do NOT wrap in `Image.clip(geometry=region)`
    # here. Each tile's per-fetch grid (affine + dimensions + crsCode)
    # already pins the exact pixels EE needs to compute; adding a clip
    # forces EE to evaluate the polygon mask against the *whole* user
    # geometry per tile — redundant work that scales with polygon
    # complexity and dominated fetch latency in the validation runs
    # (uncached EE was responding in 1.0–1.2 s clipped vs ~1.0 s
    # unclipped, but the clipped path was the one that intermittently
    # timed out at 90 s under contention). Decompose-time intersection
    # with the geometry already keeps tiles fully outside the polygon
    # out of the workload (see ``tiling.decompose_region``); edge tiles
    # return data for their full area, which callers can mask in
    # post-processing if desired. Callers who explicitly want EE-side
    # masking can .clip() the image themselves before passing it in —
    # composing with the expression is the caller's prerogative.

    # Tile
    tile_grid = decompose_region(
        geojson_geometry=geojson_geometry,
        scale_meters=scale,
        crs=crs,
        tile_size_pixels=tile_size,
        output_tile_size_pixels=output_tile_size,
    )

    # Build config — worker-pool kwargs are optional; we only override
    # the DataflowRunnerConfig defaults when the caller set them.
    if runner == "dataflow":
        df_overrides: dict[str, Any] = {}
        if machine_type is not None:
            df_overrides["machine_type"] = machine_type
        if num_workers is not None:
            df_overrides["num_workers"] = num_workers
        if max_workers is not None:
            df_overrides["max_workers"] = max_workers
        if autoscaling_algorithm is not None:
            df_overrides["autoscaling_algorithm"] = autoscaling_algorithm
        if number_of_worker_harness_threads is not None:
            df_overrides["number_of_worker_harness_threads"] = number_of_worker_harness_threads
        runner_config = RunnerConfig(
            mode="dataflow",
            dataflow=DataflowRunnerConfig(
                project=project,
                region=region_gcp,
                temp_location=temp_location,
                staging_location=temp_location.rstrip("/") + "/staging",
                labels=labels,
                **df_overrides,
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
            nodata=nodata,
            band_count=band_count,
            data_type=data_type,
        ),
        runner=runner_config,
        snapshot_time=snapshot_time_micros,
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
            snapshot_time=snapshot_time_micros,
            pixel_grid=tile_grid.pixel_grid,
            nodata=nodata,
            band_count=band_count,
            data_type=data_type,
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

    # Post-processing for local mode: count what landed on disk. Count
    # only THIS run's expected filenames — a bare glob would also count
    # leftovers from earlier runs into the same directory and mask
    # failures. `tile_grid` still holds the inline tiles even when the
    # submitted config externalized them to a file.
    tiles_ok: int | None = None
    tiles_failed: int | None = None

    if runner == "local" and not output.startswith("gs://") and tile_grid.tiles:
        output_dir = Path(output)
        two_tier = output_tile_size is not None and output_tile_size > tile_size
        keys = {(t.out_row, t.out_col) if two_tier else (t.row, t.col) for t in tile_grid.tiles}
        expected_names = {f"tile_r{r:04d}_c{c:04d}.tif" for r, c in keys}
        tiles_ok = sum(1 for name in expected_names if (output_dir / name).exists())
        tiles_failed = len(expected_names) - tiles_ok

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
        dry_run: If True, validate but don't submit. The export
            summary callback still fires; nothing is written or fetched.
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
    watchdog: Any | None = None,
    tile_count: int | None = None,
) -> Any:
    """Poll a Dataflow job until it reaches a terminal state.

    Args:
        job_id: Dataflow job ID.
        project: GCP project ID.
        region: Dataflow region (e.g. 'us-central1').
        callback: Optional callback(JobInfo) called on each poll tick.
            If None, uses Rich Live display (CLI mode).
        poll_interval: Seconds between polls.
        watchdog: :class:`datensee.status.WatchdogConfig` controlling
            cost-control circuit breakers (max_runtime, max_failure_rate,
            idle_timeout). Defaults to the package-level defaults — see
            :class:`WatchdogConfig`. Pass a custom instance to override
            individual fields, or ``WatchdogConfig(max_runtime=None, ...)``
            to disable specific checks.
        tile_count: Total compute tile count. When provided, enables the
            failure-rate circuit breaker. ``api.export()`` returns this
            on its ``ExportResult.config.tile_count``.

    Returns:
        Final JobState.

    Raises:
        WatchdogTriggered: If a watchdog policy cancels the job.
    """
    from datensee.notebook import ensure_auth

    ensure_auth()

    from datensee.auth import get_credentials
    from datensee.status import poll_job

    # Credentials (not a bare token): poll_job refreshes them per tick,
    # so multi-hour polls survive the ~1h access-token lifetime.
    return poll_job(
        job_id=job_id,
        project=project,
        region=region,
        credentials=get_credentials(),
        poll_interval_seconds=poll_interval,
        status_callback=callback,
        watchdog=watchdog,
        tile_count=tile_count,
    )


# ---------------------------------------------------------------------------
# Adaptive retry — quadtree splitter against a failures journal
# ---------------------------------------------------------------------------


class RetryResult(BaseModel):
    """Outcome of a `datensee retry` run.

    ``next_tiles_count`` is the number of TileCoordinates fed back into
    the pipeline (split children + same-pixel-rect retries). ``stats`` is
    the breakdown by action (split / retry_same / depth_cap / terminal /
    unknown_kind). ``carryover_count`` is how many original journal
    entries did not make progress this round; those records are staged to
    ``{output}/_carryover.json`` and the *pipeline* unions them into the
    new ``_failures.json`` — same code path on every runner.

    ``tiles_failed_this_round`` is the number of fresh failures the
    pipeline emitted on this attempt (journal line count minus the staged
    carryover). Set only for local mode; ``None`` when running on
    Dataflow because the pipeline writes the journal asynchronously.
    """

    job_id: str | None = None
    duration_seconds: float = 0.0
    next_tiles_count: int = 0
    carryover_count: int = 0
    stats: dict[str, int] = {}
    tiles_failed_this_round: int | None = None
    gee_project: str | None = None
    """The project the round ran under — resolved from the meta sidecar when
    the caller passed none, so CLI hints can quote it."""


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
    nodata: float | None = None,
    runner: Literal["local", "dataflow"] = "local",
    region_gcp: str = "us-central1",
    temp_location: str | None = None,
    labels: dict[str, str] | None = None,
    machine_type: str | None = None,
    num_workers: int | None = None,
    max_workers: int | None = None,
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

    The ``{output}/_export_meta.json`` sidecar written by the original
    export is **required**: journal records carry pixel offsets local to
    the original parent :class:`PixelGrid`, and only the sidecar holds
    that grid. With it present, calling ``retry(output=PATH)`` is
    sufficient — everything else is read from disk. Any explicitly-passed
    arg must match the persisted value or :class:`meta.ExportMetaMismatch`
    is raised, since a mismatch would key new COGs to a different output
    grid than the existing ones.

    Every retry round runs with ``merge_existing_output`` enabled: the
    pipeline decodes each affected output COG and overlays the re-fetched
    tiles (including quadtree split children) onto it, so
    previously-successful pixels survive the round. This holds for both
    two-tier and one-COG-per-compute-tile exports — splitting is legal
    everywhere.

    Args:
        output: Output path of the original export. Doubles as the
            anchor for the failures journal and the meta sidecar.
        journal: Path to the failures journal (NDJSON of FailedTileRecord).
            Defaults to ``{output}/_failures.json``.
        ee_expression: Same EE expression as the original export. When
            None, read from ``_export_meta.json``.
        project: Same GCP project. When None, read from meta.
        scale, crs, tile_size, output_tile_size, nodata: Must match the
            original export so split children align with the output
            grid (and re-written COGs keep their nodata tag). When
            None, read from meta.
        max_depth: Max quadtree depth. Records already at this depth
            are not split — they remain in the next round's failures.
            Default 2 (one root → 16 sub-tiles max).
        dry_run: If True, plan the retry but don't submit.
        Other args are forwarded to ``submit_job`` as in :func:`export`.

    Returns:
        RetryResult with the submitted job id (if any) plus stats.

    Raises:
        FileNotFoundError: If the ``_export_meta.json`` sidecar is
            missing from ``output``.
        ValueError: If the sidecar predates and lacks the parent
            ``pixel_grid``.
        meta.ExportMetaMismatch: If a passed arg disagrees with meta.
    """
    import logging

    from datensee.meta import (
        EXPORT_META_FILENAME,
        read_meta,
        verify_retry_compatibility,
    )
    from datensee.notebook import ensure_auth, ensure_jar
    from datensee.pixel.retry import plan_retry, read_journal, write_tiles_file
    from datensee.submit import submit_job

    if credentials is None:
        ensure_auth()

    # Resolve shape args against the persisted meta. The meta is the
    # canonical source of "what the original export was"; any explicit
    # arg the caller passes must agree with it. Retry cannot run without
    # the sidecar at all: journal records carry pixel offsets that are
    # local to the original export's parent PixelGrid, and only the meta
    # holds that grid — a guessed grid would fetch (and merge) pixels
    # from the wrong place on Earth.
    persisted_meta = read_meta(output, credentials=credentials)
    if persisted_meta is None:
        raise FileNotFoundError(
            f"{EXPORT_META_FILENAME} not found in {output!r}. Retry needs "
            "the sidecar to anchor journal tile offsets to the original "
            "export's pixel grid. Re-run the export with a current "
            "datensee version (which writes the sidecar), then retry."
        )
    if persisted_meta.pixel_grid is None:
        raise ValueError(
            f"{EXPORT_META_FILENAME} in {output!r} is missing the parent "
            "pixel_grid (written by an older datensee version), so the "
            "journal's tile offsets cannot be anchored in CRS coordinates. "
            "Re-run the original export, then retry."
        )
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
    if nodata is None:
        nodata = persisted_meta.nodata
    # Output shape declared by the original export; legacy meta → defaults.
    band_count = persisted_meta.band_count or 1
    data_type = persisted_meta.data_type or "float32"
    # Now verify everything (caller-passed values too) matches.
    verify_retry_compatibility(
        persisted_meta,
        crs=crs,
        scale_meters=scale,
        tile_size_pixels=tile_size,
        output_tile_size_pixels=output_tile_size,
        gee_project=project,
        ee_expression=ee_expression,
        nodata=nodata,
    )

    # Default the journal path to the canonical location under output —
    # read_journal handles both local paths and gs:// URIs.
    if journal is None:
        if output.startswith("gs://"):
            journal = output.rstrip("/") + "/_failures.json"
        else:
            journal = Path(output) / "_failures.json"

    records = read_journal(journal, credentials=credentials)

    # Splitting is legal for every export shape: retry rounds run with
    # merge_existing_output, so split children overlay their parent's
    # output COG in place (two-tier block or single-tile COG alike).
    plan = plan_retry(records, max_depth=max_depth)

    if not plan.next_tiles:
        return RetryResult(
            next_tiles_count=0,
            carryover_count=len(plan.carryover),
            stats=plan.stats,
            gee_project=project,
        )

    # Stage the next-round tiles file and, when this round has
    # no-progress records, the carryover journal. Both are anchored
    # under the output directory so a rerun is reproducible from the
    # journal alone. The pipeline unions the carryover with this
    # round's fresh failures when it writes _failures.json — the same
    # code path on every runner, so nothing races Dataflow's
    # asynchronous journal writer.
    def _stage(filename: str, write: Callable[[Path], None]) -> str:
        if output.startswith("gs://"):
            from datensee.submit import _upload_to_gcs

            staged_uri = output.rstrip("/") + "/" + filename
            with tempfile.TemporaryDirectory() as scratch:
                local_staging = Path(scratch) / filename
                write(local_staging)
                _upload_to_gcs(staged_uri, local_staging.read_bytes(), credentials=credentials)
            return staged_uri
        out_dir = Path(output)
        out_dir.mkdir(parents=True, exist_ok=True)
        local_path = out_dir / filename
        write(local_path)
        return str(local_path)

    tiles_file_path = _stage(
        "_retry_tiles.json", lambda path: write_tiles_file(plan.next_tiles, path)
    )

    carryover_file_path: str | None = None
    if plan.carryover:

        def _write_carryover(path: Path) -> None:
            with path.open("w", encoding="utf-8") as f:
                for record in plan.carryover:
                    f.write(json.dumps(record))
                    f.write("\n")

        carryover_file_path = _stage("_carryover.json", _write_carryover)

    # Build pipeline config with tiles_file (no inline tiles).
    if runner == "dataflow":
        # Same override surface as export(): a retry round after a
        # ZONE_RESOURCE_POOL_EXHAUSTED stockout must be able to switch
        # machine family too.
        df_overrides: dict[str, Any] = {}
        if machine_type is not None:
            df_overrides["machine_type"] = machine_type
        if num_workers is not None:
            df_overrides["num_workers"] = num_workers
        if max_workers is not None:
            df_overrides["max_workers"] = max_workers
        runner_config = RunnerConfig(
            mode="dataflow",
            dataflow=DataflowRunnerConfig(
                project=project,
                region=region_gcp,
                temp_location=temp_location or (output.rstrip("/") + "/_tmp"),
                staging_location=(temp_location or output.rstrip("/") + "/_tmp").rstrip("/")
                + "/staging",
                labels=labels,
                **df_overrides,
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
        snapshot_time_micros = persisted_meta.snapshot_time
    else:
        snapshot_time_micros = _now_micros()
        logging.getLogger(__name__).warning(
            "datensee retry: no snapshot_time in meta — original export was "
            "unpinned. Retry children will pin to %d (now). The output COGs "
            "may end up with sub-tiles fetched at different snapshots.",
            snapshot_time_micros,
        )
    ee_expression = pin_expression(ee_expression, snapshot_time_micros)

    # The retry's parent PixelGrid must match the original export's
    # exactly — col_px/row_px in the journal records are local to that
    # parent's translate_x/y. Presence was validated up front.
    parent_pixel_grid = persisted_meta.pixel_grid

    pipeline_config = PipelineConfig(
        ee_expression=ee_expression,
        gee_project=project,
        tile_grid=TileGrid(
            pixel_grid=parent_pixel_grid,
            tile_size_pixels=tile_size,
            tiles_file=tiles_file_path,
        ),
        output=OutputConfig(
            output_path=output,
            output_tile_size_pixels=output_tile_size,
            # Retry rounds overlay re-fetched tiles onto the existing
            # output COGs instead of rebuilding them from this round's
            # (partial) tile set — see AssembledCogWriter's merge path.
            merge_existing_output=True,
            nodata=nodata,
            band_count=band_count,
            data_type=data_type,
        ),
        runner=runner_config,
        snapshot_time=snapshot_time_micros,
        carryover_file=carryover_file_path,
    )

    if dry_run:
        return RetryResult(
            gee_project=project,
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

    # The pipeline itself unioned the staged carryover into the new
    # _failures.json (see DatensEEPipeline.writeFailuresJournal) — no
    # Python post-step. In local mode the pipeline has finished by the
    # time submit_job returns, so we can report this round's fresh
    # failure count: total journal lines minus the carryover we staged.
    tiles_failed_this_round: int | None = None
    if runner == "local" and not output.startswith("gs://"):
        failures_path = Path(output) / "_failures.json"
        total_lines = 0
        if failures_path.exists():
            total_lines = sum(1 for line in failures_path.read_text().splitlines() if line.strip())
        tiles_failed_this_round = max(0, total_lines - len(plan.carryover))

    return RetryResult(
        job_id=job_id,
        duration_seconds=duration,
        next_tiles_count=len(plan.next_tiles),
        carryover_count=len(plan.carryover),
        tiles_failed_this_round=tiles_failed_this_round,
        stats=plan.stats,
    )


class RetryUntilDoneResult(BaseModel):
    """Outcome of :func:`retry_until_done`.

    ``stopped`` is why the loop ended:

    * ``"no_retryable_work"`` — the journal holds nothing the policy can
      make progress on (empty, or only terminal / depth-capped records).
      This is the success-or-stuck steady state; check
      ``rounds[-1].carryover_count`` to tell the two apart.
    * ``"max_rounds"`` — the round budget ran out with retryable work
      still in the journal.
    * ``"job_<STATE>"`` — a Dataflow round ended in a non-DONE terminal
      state (e.g. ``job_JOB_STATE_FAILED``); the loop stops rather than
      resubmitting against an incomplete journal.
    """

    rounds: list[RetryResult] = []
    stopped: str = "no_retryable_work"


def retry_until_done(
    *,
    output: str,
    runner: Literal["local", "dataflow"] = "local",
    region_gcp: str = "us-central1",
    max_rounds: int = 5,
    round_backoff_seconds: float = 30.0,
    round_callback: Callable[[int, RetryResult], None] | None = None,
    credentials: Credentials | None = None,
    **retry_kwargs: Any,
) -> RetryUntilDoneResult:
    """Run :func:`retry` rounds until the journal has no retryable work.

    Each round re-reads ``{output}/_failures.json`` (which the pipeline
    keeps complete — fresh failures plus carryover), submits whatever the
    policy can make progress on, and stops when a round has nothing to
    submit or the round budget is exhausted. Transient EE weather often
    clears between rounds, so a short backoff separates them.

    In Dataflow mode each round's job is polled to a terminal state
    before the next round reads the journal (the pipeline writes
    ``_failures.json`` at job completion). A non-DONE terminal state
    stops the loop.

    Args:
        output: Output path of the original export (meta sidecar anchor).
        runner: 'local' or 'dataflow', forwarded to :func:`retry`.
        region_gcp: Dataflow region, used for polling between rounds.
        max_rounds: Hard cap on retry rounds.
        round_backoff_seconds: Sleep between rounds (skipped after the
            final round). Gives EE transients time to clear.
        round_callback: Optional ``callback(round_index, result)`` after
            each round, for progress display.
        **retry_kwargs: Forwarded to :func:`retry` (max_depth, jar,
            temp_location, ...). ``dry_run`` is not supported here.

    Returns:
        RetryUntilDoneResult with per-round results and the stop reason.
    """
    if retry_kwargs.get("dry_run"):
        raise ValueError("retry_until_done does not support dry_run — use retry().")

    rounds: list[RetryResult] = []
    for round_index in range(1, max_rounds + 1):
        result = retry(
            output=output,
            runner=runner,
            region_gcp=region_gcp,
            credentials=credentials,
            **retry_kwargs,
        )
        rounds.append(result)
        if round_callback is not None:
            round_callback(round_index, result)

        if result.next_tiles_count == 0:
            return RetryUntilDoneResult(rounds=rounds, stopped="no_retryable_work")

        if runner == "dataflow" and result.job_id is not None:
            # The journal lands when the job finishes; wait before the
            # next round reads it.
            from datensee.meta import read_meta

            meta_for_project = read_meta(output, credentials=credentials)
            project = retry_kwargs.get("project") or (
                meta_for_project.gee_project if meta_for_project else None
            )
            if project is None:
                raise ValueError(
                    "retry_until_done(runner='dataflow') needs a project to "
                    "poll the job — pass project= or keep the meta sidecar."
                )
            final_state = poll(result.job_id, project=project, region=region_gcp)
            if getattr(final_state, "name", str(final_state)) != "DONE":
                return RetryUntilDoneResult(
                    rounds=rounds,
                    stopped=f"job_{getattr(final_state, 'value', final_state)}",
                )

        if round_index < max_rounds and round_backoff_seconds > 0:
            time.sleep(round_backoff_seconds)

    return RetryUntilDoneResult(rounds=rounds, stopped="max_rounds")
