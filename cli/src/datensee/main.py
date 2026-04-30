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

import typer
from rich.console import Console

from datensee import __version__, api
from datensee.config import PipelineConfig
from datensee.display import render_export_summary, render_post_run_summary
from datensee.jar import build_jar

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
    """Parse the --snapshot-time CLI value into Unix nanos."""
    if raw is None:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    from datetime import datetime
    try:
        # Accept the trailing 'Z' shorthand for UTC.
        normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        return int(datetime.fromisoformat(normalized).timestamp() * 1_000_000_000)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--snapshot-time {raw!r} is neither Unix nanos nor ISO-8601: {exc}"
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
            help="Local directory for output tiles + VRT. Created if absent.",
        ),
    ] = Path("./datensee-output"),
    jar: Annotated[
        Path | None,
        typer.Option("--jar", help="Path to the pipeline JAR (auto-detected if omitted)."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the pipeline command without executing."),
    ] = False,
) -> None:
    """Fetch Landsat 9 NDVI tiles over SF Bay Area locally.

    Uses a hardcoded 0.25 x 0.25 degree region at 30 m/pixel (~4 tiles).
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
                "M6 two-tier tiling: output COG edge in pixels (multiple of "
                "--tile-size). Defaults to one COG per compute tile."
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
    jar: Annotated[
        Path | None,
        typer.Option("--jar", help="Path to the pipeline JAR (auto-detected if omitted)."),
    ] = None,
    max_qps: Annotated[
        int,
        typer.Option(
            "--max-qps",
            help="Max queries per second to the EE HV API (shared across all workers).",
            min=1,
        ),
    ] = 100,
    snapshot_time: Annotated[
        str | None,
        typer.Option(
            "--snapshot-time",
            help=(
                "Pin every asset reference in the EE expression to this "
                "moment. Accepts an ISO-8601 UTC timestamp (e.g. "
                "'2026-04-30T12:00:00Z') or Unix nanoseconds. Defaults to "
                "submit time. Override only to reproduce a prior export."
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the pipeline command without executing."),
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
    snapshot_time_nanos = _parse_snapshot_time(snapshot_time)

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
            runner=runner,  # type: ignore[arg-type]
            region_gcp=region_gcp,
            temp_location=temp_location,
            max_qps=max_qps,
            jar=jar,
            snapshot_time=snapshot_time_nanos,
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
        console.print(f"[green]Job submitted:[/green] {result.job_id}")

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
        from datensee.validation import validate_output

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
) -> None:
    """Poll a Dataflow job until it reaches a terminal state."""
    from datensee.auth import get_access_token
    from datensee.status import poll_job

    access_token = get_access_token()
    final_state = poll_job(
        job_id=job_id,
        project=project,
        region=region_gcp,
        access_token=access_token,
    )
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
        console.print("Build it with: datensee jar build")
        raise typer.Exit(code=1)


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


@app.command("validate")
def validate_cmd(
    output_path: Annotated[
        str,
        typer.Argument(help="Output directory (local) or GCS prefix to validate."),
    ],
    config_file: Annotated[
        Path,
        typer.Option(
            "--config",
            "-c",
            help="Pipeline config JSON file that produced the output.",
            exists=True,
            readable=True,
        ),
    ],
    checks: Annotated[
        str | None,
        typer.Option(
            "--checks",
            "-e",
            help="Comma-separated check IDs to run (e.g. E01,E03,E07). Default: all zero-cost.",
        ),
    ] = None,
    sample: Annotated[
        int,
        typer.Option("--sample", help="Tile sample size for sampling-based checks."),
    ] = 20,
    reference: Annotated[
        bool,
        typer.Option(
            "--reference",
            help="Enable E07 pixel accuracy check (costs EECUs).",
        ),
    ] = False,
    gee_project: Annotated[
        str | None,
        typer.Option("--gee-project", help="GCP project for E07 reference fetches."),
    ] = None,
    json_output: Annotated[
        Path | None,
        typer.Option("--json", help="Write machine-readable JSON report to this file."),
    ] = None,
) -> None:
    """Validate pipeline output with the DatensEE check suite.

    Runs structural, spatial, and pixel-level integration checks against
    exported tiles. By default runs all zero-cost checks (E01-E06, E08-E10).
    Use --reference to also run E07 (pixel value comparison against EE HV API).
    """
    from datensee.validation import CheckID, validate_output, zero_cost_checks

    config = PipelineConfig.read_json(config_file)

    # Parse check IDs
    check_ids: list[CheckID] | None = None
    if checks:
        check_ids = [CheckID(e.strip().upper()) for e in checks.split(",")]
    elif reference:
        check_ids = zero_cost_checks() + [CheckID.E07]

    # E07 requires a project
    if check_ids and CheckID.E07 in check_ids and not gee_project:
        project = config.gee_project
        console.print(f"[dim]Using gee_project from config: {project}[/dim]")
    else:
        project = gee_project

    report = validate_output(
        output_path,
        config,
        checks=check_ids,
        sample_size=sample,
        gee_project=project,
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
            help="M6 output tile size. Reads from meta when omitted.",
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
    max_qps: Annotated[
        int,
        typer.Option("--max-qps", help="Max QPS to the EE HV API.", min=1),
    ] = 100,
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
    from datensee.meta import ExportMetaMismatch

    ee_expression = expression_file.read_text().strip() if expression_file else None

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
            max_qps=max_qps,
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
            "[yellow]Nothing to retry — journal is empty or all entries "
            "are terminal.[/yellow]"
        )
        return

    if result.job_id:
        console.print(f"[green]Job submitted:[/green] {result.job_id}")

    if result.tiles_failed_this_round is not None:
        succeeded = result.next_tiles_count - result.tiles_failed_this_round
        console.print(
            f"[bold]This round:[/bold] {succeeded}/{result.next_tiles_count} "
            f"succeeded ({result.tiles_failed_this_round} fresh failures)"
        )
