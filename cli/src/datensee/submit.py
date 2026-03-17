"""Dataflow job submission.

Writes the pipeline config to a temp file and invokes the compiled Beam
JAR via subprocess. For local mode, runs the Direct runner in-process.

For large tile counts (>5000), uploads tile coordinates as NDJSON to the
output path and references the file in the config instead of inlining.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from rich.console import Console

from datensee.config import PipelineConfig, TileGrid

console = Console()

TILE_FILE_THRESHOLD = 5000


def submit_job(
    config: PipelineConfig,
    jar_path: Path,
    *,
    dry_run: bool = False,
) -> str | None:
    """Submit the pipeline to Dataflow (or run locally via Direct runner).

    Args:
        config: Validated pipeline configuration.
        jar_path: Path to the compiled Beam fat-JAR.
        dry_run: If True, print the command without executing it.

    Returns:
        Dataflow job ID string, or None for local runs / dry runs.

    Raises:
        FileNotFoundError: If jar_path does not exist.
        subprocess.CalledProcessError: If the pipeline invocation fails.
    """
    if not dry_run and not jar_path.exists():
        raise FileNotFoundError(
            f"Pipeline JAR not found: {jar_path}\n"
            "Run `./gradlew shadowJar` in the pipelines/ directory first."
        )

    config = _maybe_externalize_tiles(config, dry_run=dry_run)

    with tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w"
    ) as tmp:
        tmp_path = Path(tmp.name)
        config.write_json(tmp_path)

    cmd = _build_command(config, jar_path, tmp_path)

    if dry_run:
        console.print("[bold cyan]Dry run — would execute:[/bold cyan]")
        console.print(" ".join(str(c) for c in cmd))
        if config.tile_grid.tiles_file:
            console.print(
                f"[dim]Tiles would be uploaded to: {config.tile_grid.tiles_file}[/dim]"
            )
        return None

    console.print(f"[bold]Submitting pipeline[/bold] (mode={config.runner.mode})")
    console.print(f"Config written to: {tmp_path}")

    subprocess.run(cmd, check=True, text=True)

    # For local runs, job ID is not applicable.
    if config.runner.mode == "local":
        return None

    # TODO: parse Dataflow job ID from stdout/stderr.
    return None


def _maybe_externalize_tiles(
    config: PipelineConfig,
    *,
    dry_run: bool,
) -> PipelineConfig:
    """For large tile counts, write tiles to NDJSON and update config."""
    if config.tile_grid.tiles is None:
        return config
    if len(config.tile_grid.tiles) < TILE_FILE_THRESHOLD:
        return config

    tiles_file_path = _tiles_file_path(config.output.output_path)
    console.print(
        f"[bold]Externalizing {len(config.tile_grid.tiles)} tiles[/bold] → {tiles_file_path}"
    )

    if not dry_run:
        _upload_tiles_ndjson(config.tile_grid.tiles, tiles_file_path)

    new_grid = TileGrid(
        crs=config.tile_grid.crs,
        scale_meters=config.tile_grid.scale_meters,
        tile_size_pixels=config.tile_grid.tile_size_pixels,
        tiles_file=tiles_file_path,
    )

    return config.model_copy(update={"tile_grid": new_grid})


def _tiles_file_path(output_path: str) -> str:
    """Compute the NDJSON tile file path relative to the output path."""
    if output_path.startswith("gs://"):
        return output_path.rstrip("/") + "/_tiles.ndjson"
    return str(Path(output_path) / "_tiles.ndjson")


def _upload_tiles_ndjson(
    tiles: list,
    tiles_file_path: str,
) -> None:
    """Write tile coordinates as NDJSON to local path or GCS."""
    lines = [
        json.dumps(tile.model_dump(), separators=(",", ":"))
        for tile in tiles
    ]
    content = "\n".join(lines) + "\n"

    if tiles_file_path.startswith("gs://"):
        _upload_to_gcs(tiles_file_path, content.encode("utf-8"))
    else:
        path = Path(tiles_file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    console.print(f"  → wrote {len(tiles)} tile coordinates")


def _upload_to_gcs(gcs_uri: str, data: bytes) -> None:
    """Upload bytes to a GCS URI."""
    from google.cloud import storage

    parts = gcs_uri.replace("gs://", "").split("/", 1)
    bucket_name = parts[0]
    blob_name = parts[1] if len(parts) > 1 else ""

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(data, content_type="application/x-ndjson")


def _build_command(
    config: PipelineConfig,
    jar_path: Path,
    config_path: Path,
) -> list[str]:
    """Build the java invocation for the Beam pipeline."""
    cmd = [
        "java",
        "-jar",
        str(jar_path),
        f"--configFile={config_path}",
        f"--runner={'DataflowRunner' if config.runner.mode == 'dataflow' else 'DirectRunner'}",
    ]

    if config.runner.mode == "dataflow" and config.runner.dataflow is not None:
        df = config.runner.dataflow
        cmd += [
            f"--project={df.project}",
            f"--region={df.region}",
            f"--tempLocation={df.temp_location}",
            f"--stagingLocation={df.staging_location}",
            f"--workerMachineType={df.machine_type}",
            f"--maxNumWorkers={df.max_workers}",
        ]
        if df.service_account_email:
            cmd.append(f"--serviceAccount={df.service_account_email}")

    return cmd
