"""Typer CLI entrypoint for DatensEE."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from datensee import __version__
from datensee.assemble import write_vrt
from datensee.config import (
    DataflowRunnerConfig,
    OutputConfig,
    PipelineConfig,
    RunnerConfig,
)
from datensee.submit import submit_job
from datensee.tiling import decompose_region

app = typer.Typer(
    name="datensee",
    help="DatensEE: Parallelize Google Earth Engine exports via Cloud Dataflow.",
    no_args_is_help=True,
)
console = Console()

_DEFAULT_JAR = (
    Path(__file__).parents[3] / "pipelines" / "build" / "libs" / "datensee-pipeline.jar"
)

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
        Path,
        typer.Option("--jar", help="Path to the compiled pipeline JAR."),
    ] = _DEFAULT_JAR,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the pipeline command without executing."),
    ] = False,
) -> None:
    """M1 proof-of-life: fetch Landsat 9 NDVI tiles over SF Bay Area locally.

    Uses a hardcoded 0.25°×0.25° region at 30 m/pixel (~4 tiles). Output
    tiles are written to OUTPUT_DIR as individual GeoTIFFs plus a mosaic.vrt.

    To convert the VRT to a Cloud Optimized GeoTIFF:
        gdal_translate -of COG -co COMPRESS=LZW OUTPUT_DIR/mosaic.vrt ndvi.tif
    """
    output.mkdir(parents=True, exist_ok=True)

    console.print("[bold]DatensEE M1 demo[/bold] — Landsat 9 NDVI, SF Bay Area")
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

    config = PipelineConfig(
        ee_expression=_DEMO_EXPRESSION,
        gee_project=project,
        tile_grid=grid,
        output=OutputConfig(output_path=str(output.resolve())),
        runner=RunnerConfig(mode="local"),
    )

    submit_job(config, jar_path=jar, dry_run=dry_run)

    if not dry_run:
        console.print("\n[bold]Assembling VRT mosaic[/bold]")
        vrt = write_vrt(config, output)
        console.print(f"  → {vrt}")
        console.print(
            "\n[green]Done.[/green] Convert to COG with:\n"
            f"  gdal_translate -of COG -co COMPRESS=LZW {vrt} ndvi.tif"
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
        Path,
        typer.Option("--jar", help="Path to the compiled pipeline JAR."),
    ] = _DEFAULT_JAR,
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
) -> None:
    """Submit an Earth Engine export job to Cloud Dataflow (or local runner)."""
    ee_expression = expression_file.read_text().strip()
    geojson_geometry = json.loads(region_file.read_text())

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
    )

    job_id = submit_job(pipeline_config, jar_path=jar, dry_run=dry_run)
    if job_id:
        console.print(f"[green]Job submitted:[/green] {job_id}")

    if not dry_run and assemble and runner == "local" and not output.startswith("gs://"):
        console.print("\n[bold]Assembling VRT mosaic[/bold]")
        vrt = write_vrt(pipeline_config, Path(output))
        console.print(f"  → {vrt}")


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
