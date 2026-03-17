"""Dataflow job submission.

Writes the pipeline config to a temp file and invokes the compiled Beam
JAR via subprocess. For local mode, runs the Direct runner in-process.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from rich.console import Console

from datensee.config import PipelineConfig

console = Console()


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

    with tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w"
    ) as tmp:
        tmp_path = Path(tmp.name)
        config.write_json(tmp_path)

    cmd = _build_command(config, jar_path, tmp_path)

    if dry_run:
        console.print("[bold cyan]Dry run — would execute:[/bold cyan]")
        console.print(" ".join(str(c) for c in cmd))
        return None

    console.print(f"[bold]Submitting pipeline[/bold] (mode={config.runner.mode})")
    console.print(f"Config written to: {tmp_path}")

    result = subprocess.run(cmd, check=True, text=True)

    # For local runs, job ID is not applicable.
    if config.runner.mode == "local":
        return None

    # TODO: parse Dataflow job ID from stdout/stderr.
    return None


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
