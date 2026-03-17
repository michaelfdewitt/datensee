"""Typer CLI entrypoint for DatensEE."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Annotated, Any

import pyproj
import typer
from rich.console import Console

from datensee import __version__
from datensee.assemble import write_vrt
from datensee.config import (
    DataflowRunnerConfig,
    OutputConfig,
    PipelineConfig,
    RateLimitConfig,
    RunnerConfig,
)
from datensee.display import render_export_summary, render_post_run_summary
from datensee.estimate import estimate_cost
from datensee.expression import clip_expression
from datensee.jar import build_jar, download_jar, find_jar
from datensee.submit import submit_job
from datensee.tiling import decompose_region

app = typer.Typer(
    name="datensee",
    help="DatensEE: Parallelize Google Earth Engine exports via Cloud Dataflow.",
    no_args_is_help=True,
)
jar_app = typer.Typer(help="Manage the pipeline JAR (build, download, locate).")
app.add_typer(jar_app, name="jar")
console = Console()

# ---------------------------------------------------------------------------
# Hardcoded M1 demo assets
# ---------------------------------------------------------------------------

# Landsat 9 summer-2023 NDVI, serialized EE expression.
# Generated with: ee.serializer.encode(image, for_cloud_api=True) where:
#   image = (ee.ImageCollection('LANDSAT/LC09/C02/T1_L2')
#            .filterDate('2023-06-01', '2023-09-01')
#            .median()
#            .normalizedDifference(['SR_B5', 'SR_B4']))
_DEMO_EXPRESSION = json.dumps(
    {
        "result": "0",
        "values": {
            "0": {
                "functionInvocationValue": {
                    "functionName": "Image.normalizedDifference",
                    "arguments": {
                        "bandNames": {"constantValue": ["SR_B5", "SR_B4"]},
                        "input": {
                            "functionInvocationValue": {
                                "functionName": "reduce.median",
                                "arguments": {
                                    "collection": {
                                        "functionInvocationValue": {
                                            "functionName": "Collection.filter",
                                            "arguments": {
                                                "collection": {
                                                    "functionInvocationValue": {
                                                        "functionName": "ImageCollection.load",
                                                        "arguments": {
                                                            "id": {
                                                                "constantValue": "LANDSAT/LC09/C02/T1_L2"
                                                            }
                                                        },
                                                    }
                                                },
                                                "filter": {
                                                    "functionInvocationValue": {
                                                        "functionName": "Filter.dateRangeContains",
                                                        "arguments": {
                                                            "leftValue": {
                                                                "functionInvocationValue": {
                                                                    "functionName": "DateRange",
                                                                    "arguments": {
                                                                        "end": {
                                                                            "constantValue": "2023-09-01"
                                                                        },
                                                                        "start": {
                                                                            "constantValue": "2023-06-01"
                                                                        },
                                                                    },
                                                                }
                                                            },
                                                            "rightField": {
                                                                "constantValue": "system:time_start"
                                                            },
                                                        },
                                                    }
                                                },
                                            },
                                        }
                                    }
                                },
                            }
                        },
                    },
                }
            }
        },
    }
)

# 0.25° × 0.25° SF Bay Area bounding box — produces ~4 tiles at 30 m/px.
_DEMO_REGION = {
    "type": "Polygon",
    "coordinates": [
        [
            [-122.5, 37.75],
            [-122.25, 37.75],
            [-122.25, 38.0],
            [-122.5, 38.0],
            [-122.5, 37.75],
        ]
    ],
}


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
# Version callback
# ---------------------------------------------------------------------------


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"DatensEE {__version__}")
        raise typer.Exit()


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

    Uses a hardcoded 0.25 x 0.25 degree region at 30 m/pixel (~4 tiles). Output
    tiles are written to OUTPUT_DIR as individual GeoTIFFs plus a mosaic.vrt.

    To convert the VRT to a Cloud Optimized GeoTIFF:
        gdal_translate -of COG -co COMPRESS=LZW OUTPUT_DIR/mosaic.vrt ndvi.tif
    """
    import time as _time

    jar_path = find_jar(jar)

    output.mkdir(parents=True, exist_ok=True)

    console.print("[bold]DatensEE demo[/bold] — Landsat 9 NDVI, SF Bay Area")
    console.print(f"  project : {project}")
    console.print(f"  output  : {output.resolve()}")

    console.print("\n[bold]Tiling region[/bold] (30 m/px, EPSG:4326)")
    grid = decompose_region(
        geojson_geometry=_DEMO_REGION,
        scale_meters=30.0,
        crs="EPSG:4326",
        tile_size_pixels=512,
    )
    console.print(f"  → {len(grid.tiles)} tiles")

    clipped_expression = clip_expression(_DEMO_EXPRESSION, _DEMO_REGION)

    config = PipelineConfig(
        ee_expression=clipped_expression,
        gee_project=project,
        tile_grid=grid,
        output=OutputConfig(output_path=str(output.resolve())),
        runner=RunnerConfig(mode="local"),
    )

    estimate = estimate_cost(config)
    console.print(render_export_summary(config, estimate))

    t0 = _time.monotonic()
    submit_job(config, jar_path=jar_path, dry_run=dry_run)
    duration = _time.monotonic() - t0

    if not dry_run:
        console.print("\n[bold]Assembling VRT mosaic[/bold]")
        vrt = write_vrt(config, output)
        tiles_ok = len(list(output.glob("tile_*.tif")))
        console.print(render_post_run_summary(duration, tiles_ok, 0, str(vrt)))
        console.print(
            f"\nConvert to COG with:\n  gdal_translate -of COG -co COMPRESS=LZW {vrt} ndvi.tif"
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
        typer.Option("--tile-size", help="Tile edge size in pixels."),
    ] = 512,
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
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the pipeline command without executing."),
    ] = False,
    assemble: Annotated[
        bool,
        typer.Option(
            "--assemble/--no-assemble",
            help="Write a VRT mosaic after pipeline completes (local mode only).",
        ),
    ] = True,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt for large jobs."),
    ] = False,
    eecu_per_tile: Annotated[
        float,
        typer.Option(
            "--eecu-per-tile",
            help="Override EECU-seconds per tile for cost estimation (from calibration runs).",
            min=0.01,
        ),
    ] = 1.0,
    run_eval: Annotated[
        bool,
        typer.Option(
            "--eval/--no-eval",
            help="Run zero-cost evals after pipeline completes (local mode only).",
        ),
    ] = False,
) -> None:
    """Submit an Earth Engine export job to Cloud Dataflow (or local runner)."""
    import time as _time

    jar_path = find_jar(jar)

    ee_expression = expression_file.read_text().strip()
    geojson_geometry = json.loads(region_file.read_text())

    validation_errors = _validate_inputs(ee_expression, geojson_geometry, crs, output, runner)
    if validation_errors:
        for err in validation_errors:
            console.print(f"[red]Error:[/red] {err}")
        raise typer.Exit(code=1)

    # Unwrap Feature → geometry for tiling
    if geojson_geometry.get("type") == "Feature":
        geojson_geometry = geojson_geometry["geometry"]

    # Clip expression to region so edge tiles get nodata outside the boundary.
    ee_expression = clip_expression(ee_expression, geojson_geometry)

    console.print(f"[bold]Tiling region[/bold] at scale={scale}m, crs={crs}")
    tile_grid = decompose_region(
        geojson_geometry=geojson_geometry,
        scale_meters=scale,
        crs=crs,
        tile_size_pixels=tile_size,
    )
    console.print(f"  → {len(tile_grid.tiles)} tiles")

    if runner == "dataflow":
        if not temp_location:
            console.print("[red]--temp-location is required for Dataflow mode[/red]")
            raise typer.Exit(code=1)
        runner_config = RunnerConfig(
            mode="dataflow",
            dataflow=DataflowRunnerConfig(
                project=project,
                region=region_gcp,
                temp_location=temp_location,
                staging_location=temp_location.rstrip("/") + "/staging",
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

    estimate = estimate_cost(pipeline_config, eecu_per_tile=eecu_per_tile)
    console.print(render_export_summary(pipeline_config, estimate))

    # Confirm before large jobs unless --yes
    is_large = estimate.tile_count > 10_000 or (
        estimate.dataflow_cost_usd is not None and estimate.dataflow_cost_usd > 1.0
    )
    if is_large and not yes and not dry_run:
        typer.confirm("This is a large job. Proceed?", abort=True)

    t0 = _time.monotonic()
    job_id = submit_job(pipeline_config, jar_path=jar_path, dry_run=dry_run)
    duration = _time.monotonic() - t0

    if job_id:
        console.print(f"[green]Job submitted:[/green] {job_id}")

    if not dry_run and assemble and runner == "local" and not output.startswith("gs://"):
        console.print("\n[bold]Assembling VRT mosaic[/bold]")
        vrt = write_vrt(pipeline_config, Path(output))
        tiles_ok = len(list(Path(output).glob("tile_*.tif")))
        tiles_failed = max(0, estimate.tile_count - tiles_ok)
        console.print(render_post_run_summary(duration, tiles_ok, tiles_failed, str(vrt)))

    if not dry_run and run_eval and runner == "local" and not output.startswith("gs://"):
        from datensee.eval import validate_output

        console.print("\n[bold]Running evals[/bold]")
        report = validate_output(Path(output), pipeline_config)
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
        console.print("Install it with: datensee jar download  or  datensee jar build")
        raise typer.Exit(code=1)


@jar_app.command("download")
def jar_download_cmd(
    version: Annotated[
        str,
        typer.Option("--version", "-v", help="Release version to download."),
    ] = __version__,
) -> None:
    """Download a prebuilt pipeline JAR from GitHub Releases."""
    try:
        path = download_jar(version)
        console.print(f"[green]JAR ready:[/green] {path}")
    except FileNotFoundError as exc:
        console.print(f"[red]Error:[/red] {exc}")
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
# eval command
# ---------------------------------------------------------------------------


@app.command("eval")
def eval_cmd(
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
    evals: Annotated[
        str | None,
        typer.Option(
            "--evals",
            "-e",
            help="Comma-separated eval IDs to run (e.g. E01,E03,E07). Default: all zero-cost.",
        ),
    ] = None,
    sample: Annotated[
        int,
        typer.Option("--sample", help="Tile sample size for sampling-based evals."),
    ] = 20,
    reference: Annotated[
        bool,
        typer.Option(
            "--reference",
            help="Enable E07 pixel accuracy eval (costs EECUs).",
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
    """Validate pipeline output with the DatensEE eval suite.

    Runs structural, spatial, and pixel-level checks against exported tiles.
    By default runs all zero-cost evals (E01-E06, E08-E10). Use --reference
    to also run E07 (pixel value comparison against EE HV API).
    """
    from datensee.eval import EvalID, validate_output, zero_cost_evals

    config = PipelineConfig.read_json(config_file)

    # Parse eval IDs
    eval_ids: list[EvalID] | None = None
    if evals:
        eval_ids = [EvalID(e.strip().upper()) for e in evals.split(",")]
    elif reference:
        eval_ids = zero_cost_evals() + [EvalID.E07]

    # E07 requires a project
    if eval_ids and EvalID.E07 in eval_ids and not gee_project:
        project = config.gee_project
        console.print(f"[dim]Using gee_project from config: {project}[/dim]")
    else:
        project = gee_project

    report = validate_output(
        output_path,
        config,
        evals=eval_ids,
        sample_size=sample,
        gee_project=project,
    )

    console.print(report.render())

    if json_output:
        json_output.write_text(json.dumps(report.to_dict(), indent=2))
        console.print(f"[dim]Report written to {json_output}[/dim]")

    if not report.all_passed:
        raise typer.Exit(code=1)
