"""Pipeline JAR discovery and build (local mode only).

The Java Beam pipeline is required only for ``runner=local`` (Direct
runner in-process). Cloud submission goes through the Dataflow Flex
Template — no local JAR needed. See ``datensee.submit``.

Search order for ``find_jar``:

  1. Explicit path argument (e.g. from ``--jar``)
  2. ``DATENSEE_JAR`` environment variable
  3. ``~/.datensee/jars/datensee-pipeline.jar`` (user cache, populated
     by ``datensee jar build``)
  4. ``<repo>/pipelines/build/libs/datensee-pipeline.jar`` (development)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from rich.console import Console

JAR_FILENAME = "datensee-pipeline.jar"

_CACHE_DIR = Path.home() / ".datensee" / "jars"

_REPO_JAR = Path(__file__).parents[3] / "pipelines" / "build" / "libs" / JAR_FILENAME

console = Console()


def find_jar(explicit_path: Path | None = None) -> Path:
    """Locate the pipeline JAR. Raises FileNotFoundError if none exists."""
    if explicit_path is not None:
        if explicit_path.exists():
            return explicit_path
        raise FileNotFoundError(
            f"Pipeline JAR not found at explicit path: {explicit_path}\n"
            "Check the --jar path or run `datensee jar build` to compile it."
        )

    env_jar = os.environ.get("DATENSEE_JAR")
    if env_jar:
        path = Path(env_jar)
        if path.exists():
            return path
        raise FileNotFoundError(
            f"DATENSEE_JAR points to {env_jar} but the file does not exist.\n"
            "Run `datensee jar build` to compile the pipeline JAR."
        )

    cached = _CACHE_DIR / JAR_FILENAME
    if cached.exists():
        return cached

    if _REPO_JAR.exists():
        return _REPO_JAR

    raise FileNotFoundError(
        "Pipeline JAR not found. The local runner (datensee export "
        "--runner=local) needs a compiled JAR.\n"
        "  datensee jar build      Build from source (requires Java 25+ and Gradle)\n"
        "  --jar <path>            Point to an existing JAR\n"
        "Cloud mode (--runner=dataflow) does not require a local JAR."
    )


def jar_path() -> Path | None:
    """Return the JAR path if found, or None."""
    try:
        return find_jar()
    except FileNotFoundError:
        return None


def build_jar() -> Path:
    """Build the pipeline JAR from source via Gradle. Returns the JAR path."""
    pipelines_dir = Path(__file__).parents[3] / "pipelines"

    if not pipelines_dir.exists():
        raise FileNotFoundError(
            f"Pipelines directory not found at {pipelines_dir}.\n"
            "This command requires a source checkout of the datensee repository."
        )

    gradlew = pipelines_dir / "gradlew"
    if not gradlew.exists():
        raise FileNotFoundError(
            f"Gradle wrapper not found at {gradlew}.\nEnsure the repository is complete."
        )

    console.print("[bold]Building pipeline JAR...[/bold]")
    console.print(f"  {pipelines_dir}")

    subprocess.run(
        [str(gradlew), "shadowJar"],
        cwd=pipelines_dir,
        check=True,
    )

    jar = pipelines_dir / "build" / "libs" / JAR_FILENAME
    if not jar.exists():
        raise FileNotFoundError(
            f"Build succeeded but JAR not found at {jar}.\nCheck the Gradle build output."
        )

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = _CACHE_DIR / JAR_FILENAME
    shutil.copy2(jar, cached)
    console.print(f"  Cached at {cached}")

    return jar
