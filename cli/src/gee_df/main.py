"""Typer CLI entrypoint for gee-df."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from gee_df import __version__
from gee_df.config import (
    DataflowRunnerConfig,
    OutputConfig,
    PipelineConfig,
    RunnerConfig,
)
from gee_df.submit import submit_job
from gee_df.tiling import decompose_region

app = typer.Typer(
    name="gee-df",
    help="Parallelize Google Earth Engine exports via Cloud Dataflow.",
    no_args_is_help=True,
)
console = Console()

_DEFAULT_JAR = Path(__file__).parents[5] / "pipelines" / "build" / "libs" / "gee-df-pipeline.jar"


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"gee-df {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", "-V", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    pass


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
    output: Annotated[
        str,
        typer.Option("--output", "-o", help="GCS destination URI prefix."),
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
    project: Annotated[
        str | None,
        typer.Option("--project", help="GCP project ID (required for Dataflow)."),
    ] = None,
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
) -> None:
    """Submit an Earth Engine export job to Cloud Dataflow."""
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
        if not project:
            console.print("[red]--project is required for Dataflow mode[/red]")
            raise typer.Exit(code=1)
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
        tile_grid=tile_grid,
        output=OutputConfig(gcs_path=output),
        runner=runner_config,
    )

    job_id = submit_job(pipeline_config, jar_path=jar, dry_run=dry_run)
    if job_id:
        console.print(f"[green]Job submitted:[/green] {job_id}")


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
    from gee_df.status import poll_job

    # Access token sourced from ADC — placeholder for now.
    access_token = _get_access_token()
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


def _get_access_token() -> str:
    """Retrieve an OAuth2 access token from Application Default Credentials.

    TODO: Implement via google-auth library once added as a dependency.
    """
    raise NotImplementedError(
        "ADC token retrieval not yet implemented. "
        "Add google-auth to dependencies and implement here."
    )
