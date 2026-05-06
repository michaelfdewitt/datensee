"""Pipeline submission — local Direct runner and Dataflow Flex Template.

Two paths share the same ``submit_job`` entrypoint, dispatched on
``config.runner.mode``:

* ``local`` — shells out to ``java -jar <pipeline.jar>`` with the
  Direct runner. The JAR runs in-process on the user's machine; output
  is either a local directory or GCS. A Rich progress bar (or
  ``progress_callback``) tracks tile arrival. The caller's OAuth access
  token, if any, is handed to the JVM via an inheritable pipe FD so it
  never appears on argv or in the environment.

* ``dataflow`` — POSTs to the Dataflow Flex Templates ``launch``
  endpoint. The pipeline JAR lives inside a launcher container in
  Artifact Registry; the user's machine never sees it. Pipeline config
  is staged to GCS as ``{output}/_pipeline-config.json`` and the launch
  payload references that URI. Auth is ADC (or caller-supplied
  credentials) for the REST call; the Dataflow worker SA handles
  everything else (GCS writes, EE HV API).

For large tile counts (>5000), tile coordinates are written as NDJSON
to the output path and the config references the file path instead of
inlining the list.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from datensee.config import DataflowRunnerConfig, PipelineConfig, TileGrid

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

console = Console()

TILE_FILE_THRESHOLD = 5000


def _count_completed_tiles(output_dir: Path) -> int:
    """Count completed tile GeoTIFFs in the output directory."""
    return len(list(output_dir.glob("tile_*.tif")))


def submit_job(
    config: PipelineConfig,
    jar_path: Path | None = None,
    *,
    dry_run: bool = False,
    progress_callback: Callable[[int, int], None] | None = None,
    credentials: Credentials | None = None,
    template_spec: str | None = None,
) -> str | None:
    """Submit the pipeline to Dataflow (Flex Template) or run locally.

    Args:
        config: Validated pipeline configuration.
        jar_path: Path to the compiled Beam fat-JAR. Required for
            ``mode == "local"``; ignored for ``mode == "dataflow"``.
        dry_run: If True, print the planned action without executing.
        progress_callback: Optional callback(completed, total) for local mode
            progress updates. When provided, Rich progress bar is suppressed.
            Ignored for Dataflow.
        credentials: Optional caller-supplied Google credentials. Local
            mode forwards the access token to the JVM via an inheritable
            pipe FD; Dataflow mode uses them for the Flex Template launch
            REST call (and falls back to ADC when None).
        template_spec: Override for the Flex Template spec GCS URI.
            Defaults via ``template.resolve_template_spec()``.

    Returns:
        Dataflow job ID string, or None for local runs / dry runs.
    """
    if config.runner.mode == "dataflow":
        return _submit_dataflow(
            config,
            dry_run=dry_run,
            credentials=credentials,
            template_spec=template_spec,
        )

    if jar_path is None:
        raise ValueError(
            "submit_job(mode='local') requires jar_path. "
            "Run `datensee jar build` to compile the pipeline JAR."
        )
    return _submit_local(
        config,
        jar_path=jar_path,
        dry_run=dry_run,
        progress_callback=progress_callback,
        credentials=credentials,
    )


def _submit_local(
    config: PipelineConfig,
    *,
    jar_path: Path,
    dry_run: bool,
    progress_callback: Callable[[int, int], None] | None,
    credentials: Credentials | None,
) -> str | None:
    """Run the pipeline locally via the Direct runner (one JVM, in-process)."""
    if not dry_run and not jar_path.exists():
        raise FileNotFoundError(
            f"Pipeline JAR not found: {jar_path}\n"
            "Run `datensee jar build` to compile the pipeline JAR."
        )

    config = _maybe_externalize_tiles(config, dry_run=dry_run, credentials=credentials)

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tmp:
        tmp_path = Path(tmp.name)
        config.write_json(tmp_path)

    cmd = _build_local_command(jar_path, tmp_path)

    if dry_run:
        console.print("[bold cyan]Dry run — would execute:[/bold cyan]")
        console.print(" ".join(str(c) for c in cmd))
        if config.tile_grid.tiles_file:
            console.print(f"[dim]Tiles would be uploaded to: {config.tile_grid.tiles_file}[/dim]")
        return None

    console.print("[bold]Submitting pipeline[/bold] (mode=local)")
    console.print(f"Config written to: {tmp_path}")

    token_fd = _prepare_user_token_fd(credentials)
    try:
        if token_fd is not None:
            cmd = cmd + [f"--userTokenFd={token_fd}"]
        pass_fds = (token_fd,) if token_fd is not None else ()

        if not config.output.output_path.startswith("gs://"):
            _run_local_with_progress(
                cmd,
                Path(config.output.output_path),
                config.tile_count,
                progress_callback=progress_callback,
                pass_fds=pass_fds,
            )
            return None

        # GCS output, local runner: no progress bar (we can't cheaply poll
        # GCS for tile arrival). Stream stderr live and surface a tail on
        # failure.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            pass_fds=pass_fds,
            bufsize=1,
        )
        tail_lines: deque[str] = deque(maxlen=200)
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stderr.write(line)
            tail_lines.append(line.rstrip("\n"))
        sys.stderr.flush()
        returncode = proc.wait()
        if returncode != 0:
            tail = "\n".join(tail_lines).strip()
            summary = tail or f"exit {returncode} with no output"
            raise RuntimeError(f"datensee pipeline JVM failed (exit {returncode}):\n{summary}")
    finally:
        if token_fd is not None:
            try:
                os.close(token_fd)
            except OSError:
                pass

    return None


def _submit_dataflow(
    config: PipelineConfig,
    *,
    dry_run: bool,
    credentials: Credentials | None,
    template_spec: str | None,
) -> str | None:
    """Launch the pipeline via the Dataflow Flex Template REST endpoint.

    Stages the pipeline config to GCS, builds the launch payload, and
    POSTs to ``flexTemplates:launch``. Returns the job ID extracted from
    the response.
    """
    from datensee.template import resolve_template_spec

    if config.runner.dataflow is None:
        raise ValueError("Dataflow mode requires runner.dataflow config.")
    if not config.output.output_path.startswith("gs://"):
        raise ValueError(
            f"Dataflow mode requires a GCS output path, got {config.output.output_path!r}."
        )

    df = config.runner.dataflow
    spec_uri = resolve_template_spec(template_spec)

    config = _maybe_externalize_tiles(config, dry_run=dry_run, credentials=credentials)

    config_uri = config.output.output_path.rstrip("/") + "/_pipeline-config.json"
    config_json = config.model_dump_json(indent=2, exclude_none=True)

    job_name = _job_name()
    payload = _build_flex_payload(
        job_name=job_name,
        spec_uri=spec_uri,
        config_uri=config_uri,
        df=df,
    )

    if dry_run:
        console.print("[bold cyan]Dry run — would launch Flex Template:[/bold cyan]")
        console.print(f"  spec    = {spec_uri}")
        console.print(f"  config  = {config_uri}")
        console.print(f"  project = {df.project}")
        console.print(f"  region  = {df.region}")
        console.print(f"  jobName = {job_name}")
        return None

    _upload_to_gcs(
        config_uri,
        config_json.encode("utf-8"),
        credentials=credentials,
        content_type="application/json",
    )

    console.print("[bold]Submitting pipeline[/bold] (mode=dataflow, flex-template)")
    console.print(f"  spec   = {spec_uri}")
    console.print(f"  config = {config_uri}")

    job_id = _launch_flex_template(
        project=df.project,
        region=df.region,
        payload=payload,
        credentials=credentials,
    )
    console.print(f"DATENSEE_JOB_ID={job_id}")
    return job_id


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
    Raises RuntimeError with an actionable message if refresh fails so
    callers can surface it instead of inheriting an opaque transport
    error from the JVM child.
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
        raise RuntimeError("Caller-supplied credentials have no access token after refresh.")
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
        pixel_grid=config.tile_grid.pixel_grid,
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
    content_type: str = "application/x-ndjson",
) -> None:
    """Upload bytes to a GCS URI using caller-supplied credentials, if any."""
    from google.cloud import storage

    parts = gcs_uri.replace("gs://", "").split("/", 1)
    bucket_name = parts[0]
    blob_name = parts[1] if len(parts) > 1 else ""

    client = storage.Client(credentials=credentials) if credentials else storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(data, content_type=content_type)


def _build_local_command(jar_path: Path, config_path: Path) -> list[str]:
    """Build the java invocation for the Direct runner."""
    return [
        "java",
        "-jar",
        str(jar_path),
        f"--configFile={config_path}",
        "--runner=DirectRunner",
    ]


def _job_name() -> str:
    """Generate a Dataflow job name. Lowercase + dashes, ends with epoch ms."""
    return f"datensee-{int(time.time() * 1000)}"


def _build_flex_payload(
    *,
    job_name: str,
    spec_uri: str,
    config_uri: str,
    df: DataflowRunnerConfig,
) -> dict:
    """Construct the ``flexTemplates:launch`` request body.

    Custom parameters (declared in ``pipelines/metadata.json``) go in
    ``parameters``; standard Beam runtime knobs go in ``environment``.
    Anything left null is dropped — Dataflow rejects nulls on optional
    fields.

    Worker-pool throughput knobs split between two payload sections:

    * ``numWorkers``, ``maxWorkers`` are typed fields in the Flex
      Template runtime-environment proto and flow through ``environment``.
    * ``autoscalingAlgorithm`` and ``numberOfWorkerHarnessThreads`` are
      Beam pipeline options. Empirically, passing
      ``autoscalingAlgorithm`` via ``environment`` is silently dropped by
      the launcher (Dataflow runs with ``"NONE"``), so both ride in
      ``parameters`` and reach Java's ``main()`` as ``--name=value`` args
      that Beam's standard option parser picks up.

    We set everything aggressively-but-bounded by default — see
    :class:`DataflowRunnerConfig` for the rationale.
    """
    environment: dict[str, object] = {
        "tempLocation": df.temp_location,
        "stagingLocation": df.staging_location,
        "machineType": df.machine_type,
        "numWorkers": df.num_workers,
        "maxWorkers": df.max_workers,
    }
    if df.service_account_email:
        environment["serviceAccountEmail"] = df.service_account_email
    if df.network:
        environment["network"] = df.network
    if df.subnetwork:
        environment["subnetwork"] = df.subnetwork
    if df.labels:
        environment["additionalUserLabels"] = df.labels

    parameters: dict[str, object] = {
        "configFile": config_uri,
        "autoscalingAlgorithm": df.autoscaling_algorithm,
        "numberOfWorkerHarnessThreads": str(df.number_of_worker_harness_threads),
    }

    return {
        "launchParameter": {
            "jobName": job_name,
            "containerSpecGcsPath": spec_uri,
            "parameters": parameters,
            "environment": environment,
        }
    }


def _launch_flex_template(
    *,
    project: str,
    region: str,
    payload: dict,
    credentials: Credentials | None,
) -> str:
    """POST to ``flexTemplates:launch`` and return the launched job ID.

    Auth: caller-supplied credentials > ADC. The credential is refreshed
    if needed, then the bearer token is attached as an ``Authorization``
    header on a single httpx call. We don't use ``AuthorizedSession``
    because we want the existing httpx dependency to handle the
    transport — one less moving part.
    """
    import httpx
    from google.auth.transport.requests import Request

    if credentials is None:
        from google.auth import default as google_auth_default

        credentials, _ = google_auth_default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )

    if not getattr(credentials, "token", None) or getattr(credentials, "expired", False):
        credentials.refresh(Request())

    url = (
        f"https://dataflow.googleapis.com/v1b3/projects/{project}"
        f"/locations/{region}/flexTemplates:launch"
    )
    headers = {
        "Authorization": f"Bearer {credentials.token}",
        "Content-Type": "application/json",
        "x-goog-user-project": project,
    }

    response = httpx.post(url, json=payload, headers=headers, timeout=120.0)
    if response.status_code >= 400:
        raise RuntimeError(
            f"Flex Template launch failed (HTTP {response.status_code}): {response.text.strip()}"
        )

    body = response.json()
    job = body.get("job") or {}
    job_id = job.get("id")
    if not job_id:
        raise RuntimeError(f"Flex Template launch returned no job ID. Response body: {body!r}")
    return job_id
