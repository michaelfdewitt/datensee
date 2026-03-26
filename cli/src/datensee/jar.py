"""Pipeline JAR discovery and management.

The Java Beam pipeline is distributed as a fat JAR (~155 MB). This module
handles locating, downloading, and building the JAR so that the CLI can
invoke it regardless of how datensee was installed.

Search order:
  1. Explicit --jar flag (always wins)
  2. DATENSEE_JAR environment variable
  3. ~/.datensee/jars/datensee-pipeline.jar (user cache — from `datensee jar download`)
  4. Development repo path: <repo>/pipelines/build/libs/datensee-pipeline.jar
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

JAR_FILENAME = "datensee-pipeline.jar"

_CACHE_DIR = Path.home() / ".datensee" / "jars"

# Relative to main.py's location inside the installed package
_REPO_JAR = Path(__file__).parents[3] / "pipelines" / "build" / "libs" / JAR_FILENAME

_GITHUB_REPO = "michaelfdewitt/datensee"
_GITHUB_RELEASE_URL = (
    f"https://github.com/{_GITHUB_REPO}/releases/download/v{{version}}/" + JAR_FILENAME
)
_GITHUB_API_BASE = f"https://api.github.com/repos/{_GITHUB_REPO}"

console = Console()


def find_jar(explicit_path: Path | None = None) -> Path:
    """Locate the pipeline JAR, searching in priority order.

    Args:
        explicit_path: If provided, use this path directly (from --jar flag).

    Returns:
        Path to the pipeline JAR.

    Raises:
        FileNotFoundError: If no JAR can be found anywhere.
    """
    # 1. Explicit --jar flag
    if explicit_path is not None:
        if explicit_path.exists():
            return explicit_path
        raise FileNotFoundError(
            f"Pipeline JAR not found at explicit path: {explicit_path}\n"
            "Check the --jar path or run `datensee jar build` to compile it."
        )

    # 2. DATENSEE_JAR environment variable
    env_jar = os.environ.get("DATENSEE_JAR")
    if env_jar:
        path = Path(env_jar)
        if path.exists():
            return path
        raise FileNotFoundError(
            f"DATENSEE_JAR points to {env_jar} but the file does not exist.\n"
            "Run `datensee jar build` or `datensee jar download`."
        )

    # 3. User cache directory
    cached = _CACHE_DIR / JAR_FILENAME
    if cached.exists():
        return cached

    # 4. Development repo path
    if _REPO_JAR.exists():
        return _REPO_JAR

    raise FileNotFoundError(
        "Pipeline JAR not found. Install it with one of:\n"
        "  datensee jar download    Download a prebuilt JAR from GitHub Releases\n"
        "  datensee jar build       Build from source (requires Java 25+ and Gradle)\n"
        "  --jar <path>             Point to an existing JAR"
    )


def jar_path() -> Path | None:
    """Return the JAR path if found, or None."""
    try:
        return find_jar()
    except FileNotFoundError:
        return None


def _resolve_asset_url(version: str, token: str) -> str:
    """Resolve a pre-signed download URL for a release asset via the GitHub API.

    The direct ``releases/download/`` URL does not support token auth for private
    repos. The API returns a temporary pre-signed S3 URL instead.

    Args:
        version: Release version string (e.g. "0.1.0-dev").
        token: GitHub personal access token.

    Returns:
        Pre-signed S3 URL for the JAR asset.

    Raises:
        FileNotFoundError: If the release or JAR asset does not exist.
    """
    auth = {"Authorization": f"Bearer {token}"}

    release_resp = httpx.get(
        f"{_GITHUB_API_BASE}/releases/tags/v{version}",
        headers=auth,
        timeout=15,
    )
    if release_resp.status_code == 404:
        raise FileNotFoundError(
            f"No prebuilt JAR found for v{version}.\n"
            "Check available releases or build from source with `datensee jar build`."
        )
    release_resp.raise_for_status()

    asset = next(
        (a for a in release_resp.json().get("assets", []) if a["name"] == JAR_FILENAME),
        None,
    )
    if asset is None:
        raise FileNotFoundError(
            f"Release v{version} exists but contains no {JAR_FILENAME} asset.\n"
            "The release may be incomplete."
        )

    # GitHub redirects to a pre-signed S3 URL. Don't follow — S3 rejects the
    # Authorization header if it also sees the query-string signature.
    redirect = httpx.get(
        f"{_GITHUB_API_BASE}/releases/assets/{asset['id']}",
        headers={**auth, "Accept": "application/octet-stream"},
        follow_redirects=False,
        timeout=15,
    )
    if redirect.status_code not in (301, 302, 303, 307, 308):
        redirect.raise_for_status()

    return redirect.headers["location"]


def download_jar(version: str, github_token: str | None = None) -> Path:
    """Download a prebuilt pipeline JAR from GitHub Releases.

    For private repositories, pass a GitHub personal access token via
    ``github_token``, the ``GITHUB_TOKEN`` env var, or Colab Secrets.
    The token is used to resolve a pre-signed download URL via the GitHub API;
    it is never sent to S3.

    Args:
        version: Release version (e.g. "0.1.0-dev").
        github_token: GitHub personal access token.

    Returns:
        Path to the downloaded JAR.
    """
    token = github_token or os.environ.get("GITHUB_TOKEN")

    if token:
        url = _resolve_asset_url(version, token)
        stream_headers: dict[str, str] = {}  # pre-signed URL; no auth needed
    else:
        url = _GITHUB_RELEASE_URL.format(version=version)
        stream_headers = {}

    dest = _CACHE_DIR / JAR_FILENAME
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")

    console.print(f"Downloading pipeline JAR v{version}...")

    try:
        with httpx.stream("GET", url, headers=stream_headers, follow_redirects=True, timeout=300) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))

            with (
                open(tmp, "wb") as f,
                Progress(
                    TextColumn("[bold blue]{task.description}"),
                    BarColumn(),
                    DownloadColumn(),
                    TransferSpeedColumn(),
                    TimeRemainingColumn(),
                    console=console,
                ) as progress,
            ):
                task = progress.add_task("Downloading", total=total or None)
                for chunk in response.iter_bytes(chunk_size=65536):
                    f.write(chunk)
                    progress.update(task, advance=len(chunk))

        tmp.rename(dest)
        console.print(f"  Saved to {dest}")
        return dest

    except httpx.HTTPStatusError as exc:
        tmp.unlink(missing_ok=True)
        if exc.response.status_code == 404:
            raise FileNotFoundError(
                f"No prebuilt JAR found for v{version}.\n"
                "Check available releases or build from source with `datensee jar build`."
            ) from exc
        raise


def build_jar() -> Path:
    """Build the pipeline JAR from source using Gradle.

    Expects the repository structure with pipelines/ directory containing
    build.gradle.kts.

    Returns:
        Path to the built JAR.

    Raises:
        FileNotFoundError: If the pipelines directory or Gradle wrapper is not found.
        subprocess.CalledProcessError: If the build fails.
    """
    pipelines_dir = Path(__file__).parents[3] / "pipelines"

    if not pipelines_dir.exists():
        raise FileNotFoundError(
            f"Pipelines directory not found at {pipelines_dir}.\n"
            "This command requires the full datensee repository.\n"
            "Use `datensee jar download` instead if you installed via pip."
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

    # Also copy to cache dir for future use
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = _CACHE_DIR / JAR_FILENAME
    shutil.copy2(jar, cached)
    console.print(f"  Cached at {cached}")

    return jar
