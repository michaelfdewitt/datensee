"""Typer CLI entrypoint for DatensEE.

Thin wrapper around ``datensee.api``: this module owns argument parsing,
file I/O, Rich rendering, and confirmation prompts. Orchestration lives
in ``api.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console

from datensee import __version__, api
from datensee.config import PipelineConfig
from datensee.display import render_export_summary, render_post_run_summary
from datensee.jar import build_jar, download_jar

app = typer.Typer(
    name="datensee",
    help="DatensEE: Parallelize Google Earth Engine exports via Cloud Dataflow.",
    no_args_is_help=True,
)
jar_app = typer.Typer(help="Manage the local-mode pipeline JAR (build, locate).")
app.add_typer(jar_app, name="jar")
console = Console()


# ---------------------------------------------------------------------------
# Version callback
# ---------------------------------------------------------------------------


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"DatensEE {__version__}")
        raise typer.Exit()


def _parse_snapshot_time(raw: str | None) -> int | None:
    """Parse the --snapshot-time CLI value into Unix microseconds.

    Accepts ISO-8601 (`'2026-04-30T12:00:00Z'`) or an integer literal
    interpreted as Unix microseconds. The units changed in the snapshot pinning
    units fix: an old script passing nanoseconds will land in EE's
    INTERNAL-crash range and be rejected by ``pin_expression``.
    """
    if raw is None:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    from datetime import UTC, datetime

    try:
        # Accept the trailing 'Z' shorthand for UTC.
        normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            # The help text promises UTC; a naive timestamp must not be
            # reinterpreted in the machine's local timezone.
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1_000_000)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--snapshot-time {raw!r} is neither Unix micros nor ISO-8601: {exc}"
        ) from exc


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", "-V", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    pass


# ---------------------------------------------------------------------------
# demo command
# ---------------------------------------------------------------------------


def _announce_job(job_id: str, project: str, region_gcp: str) -> None:
    """Print the submitted job id and the exact command that polls it."""
    console.print(f"[green]Job submitted:[/green] {job_id}")
    console.print(
        f"  poll with: datensee status {job_id} --project {project} --region-gcp {region_gcp}"
    )


@app.command()
def demo(
    project: Annotated[
        str,
        typer.Option(
            "--project",
            "-p",
            help="GCP project ID with Earth Engine API enabled.",
        ),
    ],
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Local directory of COG tiles. Created if absent.",
        ),
    ] = Path("./datensee-output"),
    jar: Annotated[
        Path | None,
        typer.Option("--jar", help="Path to the pipeline JAR (auto-detected if omitted)."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Validate inputs and print the export summary without submitting."
        ),
    ] = False,
) -> None:
    """Fetch Landsat 9 NDVI tiles over SF Bay Area locally.

    Uses a hardcoded 0.25 x 0.25 degree region at 30 m/pixel (9 tiles of 512×512 px).
    Output COGs are written to OUTPUT_DIR as ``tile_r{row}_c{col}.tif``;
    open them as a directory in QGIS or any GIS tool. To control output
    granularity (one big COG vs. many small COGs), use ``datensee export``
    with ``--output-tile-size``.
    """
    output.mkdir(parents=True, exist_ok=True)
    output_str = str(output.resolve())

    console.print("[bold]DatensEE demo[/bold] — Landsat 9 NDVI, SF Bay Area")
    console.print(f"  project : {project}")
    console.print(f"  output  : {output.resolve()}")

    result = api.demo(
        project=project,
        output=output_str,
        jar=jar,
        dry_run=dry_run,
        confirm_callback=lambda config: console.print(render_export_summary(config)),
    )

    if not dry_run and result.duration_seconds is not None:
        console.print(
            render_post_run_summary(
                result.duration_seconds,
                result.tiles_ok or 0,
                result.tiles_failed or 0,
                output_str,
            )
        )


# ---------------------------------------------------------------------------
# export command
# ---------------------------------------------------------------------------


@app.command()
def export(
    expression_file: Annotated[
        Path,
        typer.Argument(
            help="JSON file containing the serialized EE computation expression.",
            exists=True,
            readable=True,
        ),
    ],
    region_file: Annotated[
        Path,
        typer.Argument(
            help="GeoJSON file with the export region polygon (WGS84).",
            exists=True,
            readable=True,
        ),
    ],
    project: Annotated[
        str,
        typer.Option("--project", "-p", help="GCP project ID with EE API enabled."),
    ],
    output: Annotated[
        str,
        typer.Option(
            "--output",
            "-o",
            help="Output path: GCS URI (gs://…) or local directory.",
        ),
    ],
    scale: Annotated[
        float,
        typer.Option("--scale", "-s", help="Pixel size in meters.", min=0.1),
    ] = 30.0,
    crs: Annotated[
        str,
        typer.Option("--crs", help="Target CRS (EPSG code or proj string)."),
    ] = "EPSG:4326",
    tile_size: Annotated[
        int,
        typer.Option("--tile-size", help="Compute tile edge size in pixels."),
    ] = 512,
    output_tile_size: Annotated[
        int | None,
        typer.Option(
            "--output-tile-size",
            help=(
                "two-tier tiling: output COG edge in pixels (multiple of "
                "--tile-size). Defaults to one COG per compute tile."
            ),
        ),
    ] = None,
    nodata: Annotated[
        float | None,
        typer.Option(
            "--nodata",
            help=(
                "Nodata value stamped on every output COG (GDAL_NODATA tag). "
                "EE returns masked pixels as 0 — unmask(sentinel) your "
                "expression and pass the sentinel here so GIS tools can "
                "tell nodata from real zeros."
            ),
        ),
    ] = None,
    runner: Annotated[
        str,
        typer.Option("--runner", help="Runner mode: 'local' or 'dataflow'."),
    ] = "local",
    region_gcp: Annotated[
        str,
        typer.Option("--region-gcp", help="Dataflow region."),
    ] = "us-central1",
    temp_location: Annotated[
        str | None,
        typer.Option("--temp-location", help="GCS URI for Dataflow temp files."),
    ] = None,
    machine_type: Annotated[
        str | None,
        typer.Option(
            "--machine-type",
            help=(
                "Dataflow worker machine type (default n2-standard-4). Switch "
                "families (e.g. e2-standard-4) when a zone reports "
                "ZONE_RESOURCE_POOL_EXHAUSTED."
            ),
        ),
    ] = None,
    num_workers: Annotated[
        int | None,
        typer.Option("--num-workers", min=1, help="Initial Dataflow worker count."),
    ] = None,
    max_workers: Annotated[
        int | None,
        typer.Option("--max-workers", min=1, help="Dataflow autoscaling ceiling (default 100)."),
    ] = None,
    jar: Annotated[
        Path | None,
        typer.Option("--jar", help="Path to the pipeline JAR (auto-detected if omitted)."),
    ] = None,
    snapshot_time: Annotated[
        str | None,
        typer.Option(
            "--snapshot-time",
            help=(
                "Pin every asset reference in the EE expression to this "
                "moment. Accepts an ISO-8601 UTC timestamp (e.g. "
                "'2026-04-30T12:00:00Z') or Unix microseconds. Defaults "
                "to submit time. Override only to reproduce a prior export."
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Validate inputs and print the export summary without submitting."
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt for large jobs."),
    ] = False,
    run_validate: Annotated[
        bool,
        typer.Option(
            "--validate/--no-validate",
            help="Run zero-cost output checks after pipeline completes (local mode only).",
        ),
    ] = False,
) -> None:
    """Submit an Earth Engine export job to Cloud Dataflow (or local runner)."""
    ee_expression = expression_file.read_text().strip()
    geojson_geometry = json.loads(region_file.read_text())
    snapshot_time_micros = _parse_snapshot_time(snapshot_time)

    def confirm(config: PipelineConfig) -> None:
        console.print(render_export_summary(config))
        if config.tile_count > 10_000 and not yes:
            typer.confirm("This is a large job. Proceed?", abort=True)

    try:
        result = api.export(
            ee_expression=ee_expression,
            region=geojson_geometry,
            project=project,
            output=output,
            scale=scale,
            crs=crs,
            tile_size=tile_size,
            output_tile_size=output_tile_size,
            nodata=nodata,
            runner=runner,  # type: ignore[arg-type]
            region_gcp=region_gcp,
            temp_location=temp_location,
            machine_type=machine_type,
            num_workers=num_workers,
            max_workers=max_workers,
            jar=jar,
            snapshot_time=snapshot_time_micros,
            dry_run=dry_run,
            confirm_callback=confirm,
        )
    except ValueError as exc:
        for err in str(exc).splitlines():
            console.print(f"[red]Error:[/red] {err}")
        raise typer.Exit(code=1) from exc

    # On dry-run we only see the rendered summary (printed by `confirm`); no
    # job was submitted and no post-run side effects exist to report.
    if dry_run:
        return

    if result.job_id:
        _announce_job(result.job_id, project, region_gcp)

    is_local_filesystem_output = runner == "local" and not output.startswith("gs://")

    if is_local_filesystem_output and result.duration_seconds is not None:
        console.print(
            render_post_run_summary(
                result.duration_seconds,
                result.tiles_ok or 0,
                result.tiles_failed or 0,
                output,
            )
        )

    if run_validate and is_local_filesystem_output:
        from datensee.pixel.validation import validate_output

        console.print("\n[bold]Running output checks[/bold]")
        report = validate_output(Path(output), result.config)
        console.print(report.render())
        if not report.all_passed:
            raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# status command
# ---------------------------------------------------------------------------


@app.command()
def status(
    job_id: Annotated[str, typer.Argument(help="Dataflow job ID.")],
    project: Annotated[str, typer.Option("--project", help="GCP project ID.")],
    region_gcp: Annotated[
        str,
        typer.Option("--region-gcp", help="Dataflow region."),
    ] = "us-central1",
    tile_count: Annotated[
        int | None,
        typer.Option(
            "--tile-count",
            help=(
                "Total compute tile count, used by the failure-rate "
                "watchdog. Read it off the export's `_pipeline-config.json` "
                "if you don't have it handy. Without it, the failure-rate "
                "check is skipped (max_runtime + idle_timeout still apply)."
            ),
        ),
    ] = None,
    max_runtime_hours: Annotated[
        float | None,
        typer.Option(
            "--max-runtime-hours",
            help=(
                "Hard wall-clock cap on the job. Cancels the Dataflow job "
                "and exits non-zero if exceeded. Default 12 h. Use --no-watchdog "
                "to disable all checks at once, or pass a value <= 0 to disable "
                "this specific check."
            ),
        ),
    ] = 12.0,
    max_failure_rate: Annotated[
        float | None,
        typer.Option(
            "--max-failure-rate",
            help=(
                "Cancel if (failures / tile_count) exceeds this fraction "
                "after the grace period. Default 0.5. Pass a value <= 0 to "
                "disable. Requires --tile-count."
            ),
        ),
    ] = 0.5,
    failure_grace_minutes: Annotated[
        float,
        typer.Option(
            "--failure-grace-minutes",
            help=(
                "Skip the failure-rate check for this many minutes after "
                "job start, while the worker pool is ramping up."
            ),
        ),
    ] = 10.0,
    idle_timeout_minutes: Annotated[
        float | None,
        typer.Option(
            "--idle-timeout-minutes",
            help=(
                "Cancel if no progress (no new output_tiles_written or "
                "tiles_written) for this many minutes. Default 20. Pass a "
                "value <= 0 to disable."
            ),
        ),
    ] = 20.0,
    no_watchdog: Annotated[
        bool,
        typer.Option(
            "--no-watchdog",
            help=(
                "Disable all watchdog cost controls. Use only when you "
                "intend to manage cancellation manually."
            ),
        ),
    ] = False,
) -> None:
    """Poll a Dataflow job until it reaches a terminal state.

    Applies cost-control watchdog policies by default — wall-clock cap,
    failure-rate breaker, idle-timeout — that cancel the underlying
    Dataflow job if any breach is observed. Each policy is independently
    configurable via flags above; ``--no-watchdog`` turns the whole
    apparatus off in a single switch.
    """
    from datetime import timedelta

    from datensee.status import WatchdogConfig, WatchdogTriggered

    if no_watchdog:
        watchdog = WatchdogConfig(max_runtime=None, max_failure_rate=None, idle_timeout=None)
    else:
        watchdog = WatchdogConfig(
            max_runtime=(
                timedelta(hours=max_runtime_hours)
                if max_runtime_hours is not None and max_runtime_hours > 0
                else None
            ),
            max_failure_rate=(
                max_failure_rate if max_failure_rate is not None and max_failure_rate > 0 else None
            ),
            failure_grace_period=timedelta(minutes=failure_grace_minutes),
            idle_timeout=(
                timedelta(minutes=idle_timeout_minutes)
                if idle_timeout_minutes is not None and idle_timeout_minutes > 0
                else None
            ),
        )

    try:
        final_state = api.poll(
            job_id,
            project,
            region_gcp,
            watchdog=watchdog,
            tile_count=tile_count,
        )
    except WatchdogTriggered as exc:
        console.print(f"[red]Watchdog cancelled job:[/red] {exc.reason}")
        raise typer.Exit(code=2) from exc

    if final_state.value == "JOB_STATE_DONE":
        console.print("[green]Job completed successfully.[/green]")
    else:
        console.print(f"[red]Job ended in state: {final_state.value}[/red]")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# jar subcommands
# ---------------------------------------------------------------------------


@jar_app.command("path")
def jar_path_cmd() -> None:
    """Show the path to the pipeline JAR (or error if not found)."""
    from datensee.jar import jar_path as _jar_path

    path = _jar_path()
    if path:
        console.print(str(path))
    else:
        console.print("[red]Pipeline JAR not found.[/red]")
        console.print("Fetch it with: datensee jar download  (or build: datensee jar build)")
        raise typer.Exit(code=1)


@jar_app.command("download")
def jar_download_cmd(
    version: Annotated[
        str | None,
        typer.Option("--version", help="Release version to fetch (default: this package's)."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-download even if already cached.")
    ] = False,
) -> None:
    """Download the prebuilt pipeline JAR from GitHub Releases."""
    try:
        path = download_jar(version or __version__, force=force)
        console.print(f"[green]JAR ready:[/green] {path}")
    except (FileNotFoundError, httpx.HTTPError) as exc:
        console.print(f"[red]Download failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@jar_app.command("build")
def jar_build_cmd() -> None:
    """Build the pipeline JAR from source (requires Java 25+ and Gradle)."""
    try:
        path = build_jar()
        console.print(f"[green]JAR built:[/green] {path}")
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        console.print(f"[red]Build failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc


# ---------------------------------------------------------------------------
# validate command (post-export integration test suite)
# ---------------------------------------------------------------------------


def _load_pipeline_config(reference: str) -> PipelineConfig:
    """Load a pipeline config from a local path or a ``gs://`` URI."""
    if reference.startswith("gs://"):
        from datensee.auth import gcs_client, split_gcs_uri

        bucket, blob_name = split_gcs_uri(reference)
        blob = gcs_client().bucket(bucket).blob(blob_name)
        if not blob.exists():
            raise FileNotFoundError(
                f"No pipeline config at {reference}. Pass --config with the file the export "
                "wrote (Dataflow mode stages it next to the output; local mode writes it "
                "into the output directory)."
            )
        return PipelineConfig.model_validate_json(blob.download_as_text())
    path = Path(reference)
    if not path.exists():
        raise FileNotFoundError(
            f"No pipeline config at {path}. Pass --config with the file the export wrote "
            "(local mode writes OUTPUT/_pipeline-config.json; older outputs need the "
            "temp-file path printed at submit time)."
        )
    return PipelineConfig.read_json(path)


@app.command("validate")
def validate_cmd(
    output_path: Annotated[
        str,
        typer.Argument(help="Output directory (local) or GCS prefix to validate."),
    ],
    config_file: Annotated[
        str | None,
        typer.Option(
            "--config",
            "-c",
            help="Pipeline config JSON that produced the output (local path or gs:// URI). "
            "Defaults to OUTPUT_PATH/_pipeline-config.json, which both runners write.",
        ),
    ] = None,
    pixels: Annotated[
        bool,
        typer.Option(
            "--pixels",
            help="Also re-fetch sampled tiles from the EE HV API and compare "
            "every pixel (costs EECUs; ~1 EECU-s per sampled tile).",
        ),
    ] = False,
    sample: Annotated[
        int,
        typer.Option("--sample", help="Tile sample size for the --pixels check."),
    ] = 20,
    gee_project: Annotated[
        str | None,
        typer.Option(
            "--gee-project",
            help="GCP project for --pixels re-fetches. Defaults to the config's gee_project.",
        ),
    ] = None,
    json_output: Annotated[
        Path | None,
        typer.Option("--json", help="Write machine-readable JSON report to this file."),
    ] = None,
) -> None:
    """Validate pipeline output.

    Two checks: ``integrity`` (zero-cost — every expected output COG
    exists or is journaled, with the right dimensions, CRS, and origin)
    always runs; ``--pixels`` additionally re-fetches sampled tiles from
    the EE HV API and compares every band, which verifies the entire
    pipeline chain at the cost of a few EECU-seconds.
    """
    from datensee.pixel.validation import validate_output
    from datensee.submit import PIPELINE_CONFIG_FILENAME

    config_ref = config_file or f"{output_path.rstrip('/')}/{PIPELINE_CONFIG_FILENAME}"
    try:
        config = _load_pipeline_config(config_ref)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    report = validate_output(
        output_path,
        config,
        pixels=pixels,
        sample=sample,
        gee_project=gee_project,
    )

    console.print(report.render())

    if json_output:
        json_output.write_text(json.dumps(report.to_dict(), indent=2))
        console.print(f"[dim]Report written to {json_output}[/dim]")

    if not report.all_passed:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# retry command — adaptive quadtree retry against a failures journal
# ---------------------------------------------------------------------------


@app.command("retry")
def retry_cmd(
    output: Annotated[
        str,
        typer.Option(
            "--output",
            "-o",
            help="Output path of the original export — anchors the meta "
            "sidecar and the failures journal.",
        ),
    ],
    expression_file: Annotated[
        Path | None,
        typer.Option(
            "--expression",
            "-e",
            help="JSON file with the serialized EE computation expression. "
            "Optional when _export_meta.json is present in --output.",
            exists=True,
            readable=True,
        ),
    ] = None,
    journal: Annotated[
        Path | None,
        typer.Option(
            "--journal",
            "-j",
            help="Path to the failures journal (NDJSON). Defaults to "
            "{output}/_failures.json for local outputs.",
            exists=True,
            readable=True,
        ),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option(
            "--project",
            "-p",
            help="GCP project ID. Optional when _export_meta.json is present.",
        ),
    ] = None,
    scale: Annotated[
        float | None,
        typer.Option(
            "--scale",
            "-s",
            help="Pixel size in meters. Reads from meta when omitted.",
            min=0.1,
        ),
    ] = None,
    crs: Annotated[
        str | None,
        typer.Option("--crs", help="Target CRS. Reads from meta when omitted."),
    ] = None,
    tile_size: Annotated[
        int | None,
        typer.Option(
            "--tile-size",
            help="Compute tile edge in pixels. Reads from meta when omitted.",
        ),
    ] = None,
    output_tile_size: Annotated[
        int | None,
        typer.Option(
            "--output-tile-size",
            help="two-tier output tile size. Reads from meta when omitted.",
        ),
    ] = None,
    runner: Annotated[
        str,
        typer.Option("--runner", help="Runner mode: 'local' or 'dataflow'."),
    ] = "local",
    region_gcp: Annotated[
        str,
        typer.Option("--region-gcp", help="Dataflow region."),
    ] = "us-central1",
    temp_location: Annotated[
        str | None,
        typer.Option("--temp-location", help="GCS URI for Dataflow temp files."),
    ] = None,
    machine_type: Annotated[
        str | None,
        typer.Option(
            "--machine-type",
            help="Dataflow worker machine type (default n2-standard-4); e.g. e2-standard-4 "
            "when a zone reports ZONE_RESOURCE_POOL_EXHAUSTED.",
        ),
    ] = None,
    num_workers: Annotated[
        int | None,
        typer.Option("--num-workers", min=1, help="Initial Dataflow worker count."),
    ] = None,
    max_workers: Annotated[
        int | None,
        typer.Option("--max-workers", min=1, help="Dataflow autoscaling ceiling (default 100)."),
    ] = None,
    max_depth: Annotated[
        int,
        typer.Option(
            "--max-depth",
            help="Maximum quadtree depth. Tiles already at this depth in their "
            "lineage are not split (they remain in the next round's failures).",
            min=0,
            max=6,
        ),
    ] = 2,
    jar: Annotated[
        Path | None,
        typer.Option("--jar", help="Path to the pipeline JAR (auto-detected if omitted)."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Plan the retry but don't submit."),
    ] = False,
    until_done: Annotated[
        bool,
        typer.Option(
            "--until-done",
            help="Loop retry rounds until the journal has no retryable "
            "work (or --max-rounds is hit). Between rounds, Dataflow "
            "jobs are polled to completion.",
        ),
    ] = False,
    max_rounds: Annotated[
        int,
        typer.Option(
            "--max-rounds",
            help="Round budget for --until-done.",
            min=1,
            max=25,
        ),
    ] = 5,
    round_backoff: Annotated[
        float,
        typer.Option(
            "--round-backoff",
            help="Seconds to wait between --until-done rounds (lets EE transients clear).",
            min=0.0,
        ),
    ] = 30.0,
) -> None:
    """Re-submit failed tiles from a journal, splitting where appropriate.

    Reads the failures journal written by a prior pipeline run,
    classifies each entry by ``error_kind``, and for EE-specific
    complexity errors (``MEMORY_EXCEEDED``, ``COMPUTATION_TIMEOUT``)
    emits 4 quadtree children. Transient infrastructure errors
    (rate-limit, generic 5xx, unknown) retry the same bbox. Auth and
    fatal errors are dropped from the retry stream and surfaced to the
    user.

    With ``_export_meta.json`` in the output directory (written by every
    export from this version of datensee on), retry needs no shape args:

        datensee retry --output ./my-export

    will read the original CRS, scale, tile sizes, project, and
    expression from the sidecar. Any explicitly-passed shape arg must
    match the persisted value or the command refuses to run, since a
    mismatch would key new COGs to a different output grid than the
    existing ones.
    """
    from datensee.api import retry as run_retry
    from datensee.api import retry_until_done
    from datensee.meta import ExportMetaMismatch

    ee_expression = expression_file.read_text().strip() if expression_file else None

    if until_done and dry_run:
        console.print("[red]--until-done cannot be combined with --dry-run.[/red]")
        raise typer.Exit(code=1)

    if until_done:

        def _report_round(round_index: int, round_result) -> None:
            if round_result.next_tiles_count == 0:
                console.print(
                    f"[bold]Round {round_index}[/bold]: nothing left to retry "
                    f"({round_result.carryover_count} record(s) carried over)"
                )
                return
            fresh = round_result.tiles_failed_this_round
            console.print(
                f"[bold]Round {round_index}[/bold]: submitted "
                f"{round_result.next_tiles_count} tiles, "
                f"{fresh if fresh is not None else '?'} fresh failures, "
                f"{round_result.carryover_count} carried over"
            )

        try:
            loop_result = retry_until_done(
                output=output,
                runner=runner,  # type: ignore[arg-type]
                region_gcp=region_gcp,
                max_rounds=max_rounds,
                round_backoff_seconds=round_backoff,
                round_callback=_report_round,
                journal=journal,
                ee_expression=ee_expression,
                project=project,
                scale=scale,
                crs=crs,
                tile_size=tile_size,
                output_tile_size=output_tile_size,
                temp_location=temp_location,
                machine_type=machine_type,
                num_workers=num_workers,
                max_workers=max_workers,
                jar=jar,
                max_depth=max_depth,
            )
        except (ExportMetaMismatch, FileNotFoundError, ValueError) as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(code=1) from exc

        last = loop_result.rounds[-1]
        if loop_result.stopped == "no_retryable_work":
            if last.carryover_count == 0:
                console.print(
                    f"[green]Journal clear after {len(loop_result.rounds)} round(s) — "
                    "all tiles recovered.[/green]"
                )
            else:
                console.print(
                    f"[yellow]{last.carryover_count} record(s) remain stuck "
                    f"(terminal or depth-capped) after {len(loop_result.rounds)} "
                    "round(s). Inspect _failures.json; bumping --max-depth may "
                    "rescue depth-capped tiles.[/yellow]"
                )
        elif loop_result.stopped == "max_rounds":
            console.print(
                f"[yellow]Round budget ({max_rounds}) exhausted with retryable "
                "work remaining — re-run to continue.[/yellow]"
            )
            raise typer.Exit(code=1)
        else:
            console.print(
                f"[red]Stopped: {loop_result.stopped}. Fix the job failure, then re-run.[/red]"
            )
            raise typer.Exit(code=1)
        return

    try:
        result = run_retry(
            output=output,
            journal=journal,
            ee_expression=ee_expression,
            project=project,
            scale=scale,
            crs=crs,
            tile_size=tile_size,
            output_tile_size=output_tile_size,
            runner=runner,  # type: ignore[arg-type]
            region_gcp=region_gcp,
            temp_location=temp_location,
            machine_type=machine_type,
            num_workers=num_workers,
            max_workers=max_workers,
            jar=jar,
            max_depth=max_depth,
            dry_run=dry_run,
        )
    except ExportMetaMismatch as exc:
        for line in str(exc).splitlines():
            console.print(f"[red]{line}[/red]" if line.strip() else "")
        raise typer.Exit(code=1) from exc
    except FileNotFoundError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print("[bold]Retry plan[/bold]")
    for action, count in sorted(result.stats.items()):
        console.print(f"  {action}: {count}")
    console.print(f"  → {result.next_tiles_count} tiles to fetch")
    console.print(f"  → {result.carryover_count} carried over (depth-capped or terminal)")

    if dry_run:
        console.print("[dim]Dry-run; no job submitted.[/dim]")
        return

    if result.next_tiles_count == 0:
        console.print(
            "[yellow]Nothing to retry — journal is empty or all entries are terminal.[/yellow]"
        )
        return

    if result.job_id:
        # --project is optional for retry (the meta sidecar supplies it);
        # quote the project the round actually ran under.
        _announce_job(result.job_id, result.gee_project or project or "<project>", region_gcp)

    if result.tiles_failed_this_round is not None:
        succeeded = result.next_tiles_count - result.tiles_failed_this_round
        console.print(
            f"[bold]This round:[/bold] {succeeded}/{result.next_tiles_count} "
            f"succeeded ({result.tiles_failed_this_round} fresh failures)"
        )
