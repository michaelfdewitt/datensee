"""Dataflow job submission.

Writes the pipeline config to a temp file and invokes the compiled Beam
JAR via subprocess. For local mode, runs the Direct runner in-process
with a Rich progress bar driven by output file polling.

For large tile counts (>5000), uploads tile coordinates as NDJSON to the
output path and references the file in the config instead of inlining.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from datensee.config import PipelineConfig, TileGrid

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

console = Console()

TILE_FILE_THRESHOLD = 5000


def _count_completed_tiles(output_dir: Path) -> int:
    """Count completed tile GeoTIFFs in the output directory."""
    return len(list(output_dir.glob("tile_*.tif")))


def submit_job(
    config: PipelineConfig,
    jar_path: Path,
    *,
    dry_run: bool = False,
    progress_callback: Callable[[int, int], None] | None = None,
    credentials: Credentials | None = None,
) -> str | None:
    """Submit the pipeline to Dataflow (or run locally via Direct runner).

    Args:
        config: Validated pipeline configuration.
        jar_path: Path to the compiled Beam fat-JAR.
        dry_run: If True, print the command without executing it.
        progress_callback: Optional callback(completed, total) for local mode
            progress updates. When provided, Rich progress bar is suppressed.
            When None, Rich progress bar is used (backwards-compatible).
        credentials: Optional caller-supplied Google credentials. When set,
            the access token is handed to the Java subprocess via an
            inheritable pipe FD (`--userTokenFd=<N>`) so the token never
            appears on argv or in the subprocess environment. Also used for
            driver-side GCS uploads.

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

    config = _maybe_externalize_tiles(config, dry_run=dry_run, credentials=credentials)

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tmp:
        tmp_path = Path(tmp.name)
        config.write_json(tmp_path)

    cmd = _build_command(config, jar_path, tmp_path)

    if dry_run:
        console.print("[bold cyan]Dry run — would execute:[/bold cyan]")
        console.print(" ".join(str(c) for c in cmd))
        if config.tile_grid.tiles_file:
            console.print(f"[dim]Tiles would be uploaded to: {config.tile_grid.tiles_file}[/dim]")
        return None

    console.print(f"[bold]Submitting pipeline[/bold] (mode={config.runner.mode})")
    console.print(f"Config written to: {tmp_path}")

    token_fd = _prepare_user_token_fd(credentials)
    try:
        if token_fd is not None:
            cmd = cmd + [f"--userTokenFd={token_fd}"]
        pass_fds = (token_fd,) if token_fd is not None else ()

        if config.runner.mode == "local" and not config.output.output_path.startswith("gs://"):
            _run_local_with_progress(
                cmd,
                Path(config.output.output_path),
                config.tile_count,
                progress_callback=progress_callback,
                pass_fds=pass_fds,
            )
            return None

        try:
            subprocess.run(
                cmd,
                check=True,
                text=True,
                pass_fds=pass_fds,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            # Tee the child's streams to our own so Cloud Run / CLI logs
            # still carry the full stack trace, then raise a rich
            # RuntimeError whose message contains the tail — otherwise the
            # caller only sees argv + exit code, which is useless.
            if exc.stdout:
                sys.stderr.write(exc.stdout)
            if exc.stderr:
                sys.stderr.write(exc.stderr)
            sys.stderr.flush()
            combined = (exc.stderr or "") + (exc.stdout or "")
            tail = "\n".join(combined.splitlines()[-40:]).strip()
            summary = tail or f"exit {exc.returncode} with no output"
            raise RuntimeError(
                f"datensee pipeline JVM failed (exit {exc.returncode}):\n{summary}"
            ) from exc
    finally:
        # Parent-side close of the read end (the child has inherited its
        # own copy). If the child never ran — spawn failure, early raise —
        # this still cleans up the FD.
        if token_fd is not None:
            try:
                os.close(token_fd)
            except OSError:
                pass

    # For local runs, job ID is not applicable.
    if config.runner.mode == "local":
        return None

    # TODO: parse Dataflow job ID from stdout/stderr.
    return None


def _prepare_user_token_fd(credentials: Credentials | None) -> int | None:
    """Write the caller's access token onto a pipe and return the read-end FD.

    Creates an `os.pipe()`, writes the token to the write-end, closes the
    write-end (so the child hits EOF after reading), and marks the read-end
    inheritable so `subprocess.Popen(pass_fds=...)` keeps it open across the
    fork+exec. Returns the read-end FD — the caller is responsible for
    passing it via `pass_fds` and closing it after the child has exited.

    The whole point of this dance is to avoid putting the bearer token on
    argv or in the subprocess environment, where it would be visible in
    `/proc/<pid>/cmdline` and `/proc/<pid>/environ`. The FD number itself
    is fine to expose on argv — it's a small integer that means nothing
    outside this process tree.
    """
    if credentials is None:
        return None
    token = _materialize_access_token(credentials)
    # bytearray is mutable, so we can zero the buffer after os.write.
    # The underlying str `token` is still in memory until GC — we can't
    # fix that without reaching into CPython internals, and that's not
    # worth the maintenance cost.
    token_bytes = bytearray(token.encode("utf-8"))
    read_fd, write_fd = os.pipe()
    try:
        os.set_inheritable(read_fd, True)
        os.write(write_fd, token_bytes)
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    finally:
        for i in range(len(token_bytes)):
            token_bytes[i] = 0
    # Close the write end so the child's read hits EOF after the token
    # bytes are consumed.
    os.close(write_fd)
    return read_fd


def _materialize_access_token(credentials: Credentials) -> str:
    """Return a live access token from a Credentials object.

    Refreshes the credential if it is missing a token or has expired.
    Raises RuntimeError with an actionable message if refresh fails —
    the caller (FoundrEE bridge) re-emits this as an export-failed event.
    """
    token = getattr(credentials, "token", None)
    expired = getattr(credentials, "expired", False)
    if token is None or expired:
        try:
            from google.auth.transport.requests import Request

            credentials.refresh(Request())
        except Exception as exc:
            raise RuntimeError(
                f"Failed to refresh caller-supplied credentials: {exc}. "
                "The access token is missing or expired and could not be renewed."
            ) from exc
        token = credentials.token
    if not token:
        raise RuntimeError(
            "Caller-supplied credentials have no access token after refresh."
        )
    return token


def _run_local_with_progress(
    cmd: list[str],
    output_dir: Path,
    total_tiles: int,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
    pass_fds: tuple[int, ...] = (),
) -> None:
    """Run the local pipeline with progress tracking driven by file polling.

    Spawns the Java process and polls output_dir for tile_*.tif files every
    0.5s. When progress_callback is provided, calls it with (completed, total)
    instead of rendering a Rich progress bar.

    Args:
        cmd: Java command to execute.
        output_dir: Directory where tile GeoTIFFs are written.
        total_tiles: Expected number of tiles (for the progress bar total).
        progress_callback: Optional callback(completed, total). When provided,
            Rich progress bar is suppressed.

    Raises:
        subprocess.CalledProcessError: If the Java process exits non-zero.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        pass_fds=pass_fds,
    )

    if progress_callback is not None:
        while process.poll() is None:
            completed = _count_completed_tiles(output_dir)
            progress_callback(min(completed, total_tiles or completed), total_tiles)
            time.sleep(0.5)
        completed = _count_completed_tiles(output_dir)
        progress_callback(min(completed, total_tiles or completed), total_tiles)
    else:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("{task.completed}/{task.total} tiles"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Fetching tiles", total=total_tiles or 1)

            while process.poll() is None:
                completed = _count_completed_tiles(output_dir)
                progress.update(task, completed=min(completed, total_tiles or completed))
                time.sleep(0.5)

            completed = _count_completed_tiles(output_dir)
            progress.update(task, completed=min(completed, total_tiles or completed))

    if process.returncode != 0:
        stderr = process.stderr.read() if process.stderr else ""
        raise subprocess.CalledProcessError(process.returncode, cmd, output="", stderr=stderr)


def _maybe_externalize_tiles(
    config: PipelineConfig,
    *,
    dry_run: bool,
    credentials: Credentials | None = None,
) -> PipelineConfig:
    """For large tile counts, write tiles to NDJSON and update config."""
    if config.tile_grid.tiles is None:
        return config
    if config.tile_count < TILE_FILE_THRESHOLD:
        return config

    tiles_file_path = _tiles_file_path(config.output.output_path)
    console.print(f"[bold]Externalizing {config.tile_count} tiles[/bold] → {tiles_file_path}")

    if not dry_run:
        _upload_tiles_ndjson(config.tile_grid.tiles, tiles_file_path, credentials=credentials)

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
    *,
    credentials: Credentials | None = None,
) -> None:
    """Write tile coordinates as NDJSON to local path or GCS."""
    lines = [json.dumps(tile.model_dump(), separators=(",", ":")) for tile in tiles]
    content = "\n".join(lines) + "\n"

    if tiles_file_path.startswith("gs://"):
        _upload_to_gcs(tiles_file_path, content.encode("utf-8"), credentials=credentials)
    else:
        path = Path(tiles_file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    console.print(f"  → wrote {len(tiles)} tile coordinates")


def _upload_to_gcs(
    gcs_uri: str,
    data: bytes,
    *,
    credentials: Credentials | None = None,
) -> None:
    """Upload bytes to a GCS URI using caller-supplied credentials, if any."""
    from google.cloud import storage

    parts = gcs_uri.replace("gs://", "").split("/", 1)
    bucket_name = parts[0]
    blob_name = parts[1] if len(parts) > 1 else ""

    client = storage.Client(credentials=credentials) if credentials else storage.Client()
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
        if df.labels:
            # Beam's PipelineOptionsFactory parses --labels as a JSON map
            # onto DataflowPipelineWorkerPoolOptions.setLabels(Map<String,String>).
            cmd.append(f"--labels={json.dumps(df.labels, separators=(',', ':'))}")

    return cmd
