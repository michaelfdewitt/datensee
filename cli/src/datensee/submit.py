"""Pipeline submission: local Direct runner and Dataflow Flex Template.

Two paths share the same ``submit_job`` entrypoint, dispatched on
``config.runner.mode``:

* ``local``: Executes ``java -jar <pipeline.jar>`` with the Direct runner.
  The JAR runs in-process on the local machine; output is written to a local
  directory or GCS. A progress display tracks tile arrival.

* ``dataflow``: Submits to the Dataflow Flex Templates ``launch`` endpoint.
  The pipeline JAR runs within a container in Artifact Registry. Pipeline
  configuration is staged to GCS as ``{output}/_pipeline-config.json``.
  Authentication uses ADC or provided credentials for the submission API call;
  the Dataflow worker service account handles GCS and Earth Engine requests.

For large tile counts (>5000), tile coordinates are staged as NDJSON
to the output path and referenced by file path in the configuration.
"""

from __future__ import annotations

import json
import re
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


PIPELINE_CONFIG_FILENAME = "_pipeline-config.json"
"""Config sidecar written next to the output so ``datensee validate`` can find it."""

_MIN_JAVA_MAJOR = 21
"""The Dataflow worker harness is Java 21, so the JAR is built for it (``--release 21``)."""


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

    # Local-filesystem outputs get the config as a sidecar next to the
    # COGs (mirroring the gs://…/_pipeline-config.json that Dataflow mode
    # stages), so `datensee validate OUTPUT` needs no --config. A gs://
    # output in local mode keeps a temp file.
    if config.output.output_path.startswith("gs://"):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tmp:
            tmp_path = Path(tmp.name)
    else:
        out_dir = Path(config.output.output_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = out_dir / PIPELINE_CONFIG_FILENAME
    config.write_json(tmp_path)

    cmd = _build_local_command(jar_path, tmp_path)

    if dry_run:
        console.print("[bold cyan]Dry run: would execute:[/bold cyan]")
        console.print(" ".join(str(c) for c in cmd))
        if config.tile_grid.tiles_file:
            console.print(f"[dim]Tiles would be uploaded to: {config.tile_grid.tiles_file}[/dim]")
        return None

    _require_java()
    console.print("[bold]Submitting pipeline[/bold] (mode=local)")
    console.print(f"Config written to: {tmp_path}")

    if not config.output.output_path.startswith("gs://"):
        _run_local_with_progress(
            cmd,
            Path(config.output.output_path),
            config.tile_count,
            progress_callback=progress_callback,
        )
        return None

    # GCS output, local runner: no progress bar (we can't cheaply poll
    # GCS for tile arrival). Stream stderr live and surface a tail on failure.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
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

    config_uri = config.output.output_path.rstrip("/") + "/" + PIPELINE_CONFIG_FILENAME
    config_json = config.model_dump_json(indent=2, exclude_none=True)

    job_name = _job_name()
    payload = _build_flex_payload(
        job_name=job_name,
        spec_uri=spec_uri,
        config_uri=config_uri,
        df=df,
    )

    if dry_run:
        console.print("[bold cyan]Dry run: would launch Flex Template:[/bold cyan]")
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


def _run_local_with_progress(
    cmd: list[str],
    output_dir: Path,
    total_tiles: int,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
) -> None:
    """Run the local pipeline with progress tracking driven by file polling.

    Spawns the Java process and polls output_dir for tile_*.tif files every
    0.5s. When progress_callback is provided, calls it with (completed, total)
    instead of rendering a Rich progress bar.

    The JVM's stdout/stderr are continuously drained by a background
    thread into a bounded tail. Without the drain, Beam's per-tile INFO
    logging fills the OS pipe buffer (~64KB), the JVM blocks on write,
    and the export hangs forever while this loop keeps polling a
    process that can never finish.

    Args:
        cmd: Java command to execute.
        output_dir: Directory where tile GeoTIFFs are written.
        total_tiles: Expected number of tiles (for the progress bar total).
        progress_callback: Optional callback(completed, total). When provided,
            Rich progress bar is suppressed.

    Raises:
        subprocess.CalledProcessError: If the Java process exits non-zero.
    """
    import threading

    output_dir.mkdir(parents=True, exist_ok=True)

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    tail_lines: deque[str] = deque(maxlen=200)

    def _drain() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            tail_lines.append(line.rstrip("\n"))

    drain_thread = threading.Thread(target=_drain, name="datensee-jvm-drain", daemon=True)
    drain_thread.start()

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

    drain_thread.join(timeout=5.0)
    if process.returncode != 0:
        raise subprocess.CalledProcessError(
            process.returncode, cmd, output="", stderr="\n".join(tail_lines)
        )


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

    # `tile_grid` lives inside `pixel:` now; replace the whole payload so
    # the model_copy update walks the nested record.
    new_pixel = config.pixel.model_copy(update={"tile_grid": new_grid})
    return config.model_copy(update={"pixel": new_pixel})


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
    from datensee.auth import gcs_client, split_gcs_uri

    bucket_name, blob_name = split_gcs_uri(gcs_uri)
    bucket = gcs_client(credentials).bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(data, content_type=content_type)


def _build_local_command(jar_path: Path, config_path: Path) -> list[str]:
    """Build the java invocation for the Direct runner."""
    return [
        "java",
        # Log lines carry non-ASCII; without an explicit encoding a JVM on
        # a host with no UTF-8 locale (bare containers) mangles them to '?'.
        # (Native-access policy lives in the JAR manifest, not here.)
        "-Dstdout.encoding=UTF-8",
        "-Dstderr.encoding=UTF-8",
        "-jar",
        str(jar_path),
        f"--configFile={config_path}",
        "--runner=DirectRunner",
    ]


def _parse_java_major(version_output: str) -> int | None:
    """Extract the major version from ``java -version`` output.

    Handles both the modern ``"21.0.4"`` and the legacy ``"1.8.0_392"``
    spellings. Returns ``None`` when no version string is present.
    """
    match = re.search(r'version "(\d+)(?:\.(\d+))?', version_output)
    if match is None:
        return None
    major = int(match.group(1))
    if major == 1 and match.group(2):
        return int(match.group(2))
    return major


def _require_java(minimum_major: int = _MIN_JAVA_MAJOR) -> None:
    """Fail before spawning the JVM if ``java`` is missing or too old.

    The JAR is compiled for Java 21 (class-file 65); an older launcher
    would die with an opaque ``UnsupportedClassVersionError``.

    Raises:
        RuntimeError: With install guidance, when ``java`` is absent or
            older than ``minimum_major``.
    """
    try:
        probe = subprocess.run(["java", "-version"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise RuntimeError(
            "`java` was not found on PATH. The local runner needs a Java "
            f"{minimum_major}+ runtime (e.g. Temurin); install one, or use "
            "--runner=dataflow, which needs no local Java."
        ) from None
    major = _parse_java_major(probe.stderr + probe.stdout)
    if major is not None and major < minimum_major:
        raise RuntimeError(
            f"Java {major} found on PATH, but the pipeline JAR needs Java "
            f"{minimum_major}+ (the Dataflow worker JDK). Install a newer JDK/JRE or "
            "point PATH/JAVA_HOME at one."
        )


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
    Null values are omitted because Dataflow rejects nulls on optional
    fields.

    Worker-pool throughput settings split between two payload sections:

    * ``numWorkers`` and ``maxWorkers`` are typed fields in the Flex
      Template runtime-environment proto and flow through ``environment``.
    * ``autoscalingAlgorithm`` and ``numberOfWorkerHarnessThreads`` are
      Beam pipeline options. Empirically, passing
      ``autoscalingAlgorithm`` via ``environment`` is dropped by the launcher,
      so both ride in ``parameters`` and reach Java's ``main()`` as command-line
      arguments that Beam's option parser handles.

    Default settings balance throughput with bounded worker resource usage; see
    :class:`DataflowRunnerConfig` for details.
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
    header on an httpx request.
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
