"""Pipeline JAR discovery, download, and build (local mode only).

The Java Beam pipeline is required only for ``runner=local`` (Direct
runner in-process). Cloud submission goes through the Dataflow Flex
Template — no local JAR needed. See ``datensee.submit``.

Search order for ``find_jar``:

  1. Explicit path argument (e.g. from ``--jar``)
  2. ``DATENSEE_JAR`` environment variable
  3. ``~/.datensee/jars/datensee-pipeline-<version>.jar`` (populated by
     ``datensee jar download``, or automatically on first local export)
  4. ``~/.datensee/jars/datensee-pipeline.jar`` (populated by
     ``datensee jar build``)
  5. ``<repo>/pipelines/build/libs/datensee-pipeline.jar`` (development)

``ensure_jar`` is ``find_jar`` plus a fallback download of the prebuilt
JAR attached to the matching GitHub Release (tag ``v<version>``). For a
private repository, set ``GITHUB_TOKEN`` (or ``DATENSEE_GITHUB_TOKEN``);
the token resolves a pre-signed asset URL via the GitHub API and is never
sent to the download host.
"""

from __future__ import annotations

import hashlib
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

from datensee._jar_digest import JAR_SHA256
from datensee._version import __version__
from datensee.template import TEMPLATE_BUCKET

JAR_FILENAME = "datensee-pipeline.jar"

# Fallback host: the same public bucket that serves the Flex Template
# specs. Versioned path — a new release writes a new prefix, so nothing
# that can change under an existing install; a .sha256 sidecar pins the
# bytes and is verified after download.
_GCS_JAR_URL = f"https://storage.googleapis.com/{TEMPLATE_BUCKET}/v{{version}}/{JAR_FILENAME}"

GITHUB_REPOSITORY = "michaelfdewitt/datensee"

_GITHUB_RELEASE_ASSET_URL = (
    f"https://github.com/{GITHUB_REPOSITORY}/releases/download/v{{version}}/{JAR_FILENAME}"
)
_GITHUB_API_BASE = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"

_CACHE_DIR = Path.home() / ".datensee" / "jars"

_REPO_JAR = Path(__file__).parents[3] / "pipelines" / "build" / "libs" / JAR_FILENAME

console = Console()


def versioned_jar_filename(version: str = __version__) -> str:
    """Cache filename for the prebuilt JAR of a given release."""
    return f"datensee-pipeline-{version}.jar"


def _not_found_message() -> str:
    return (
        "Pipeline JAR not found. The local runner (datensee export "
        "--runner=local) needs a compiled JAR.\n"
        f"  datensee jar download   Fetch the prebuilt JAR for v{__version__} "
        "from GitHub Releases\n"
        "  datensee jar build      Build from source (requires a repo checkout, "
        "Java 25+ and Gradle)\n"
        "  --jar <path>            Point to an existing JAR\n"
        "Cloud mode (--runner=dataflow) does not require a local JAR."
    )


def find_jar(explicit_path: Path | None = None) -> Path:
    """Locate the pipeline JAR on disk. Raises FileNotFoundError if none exists."""
    if explicit_path is not None:
        if explicit_path.exists():
            return explicit_path
        raise FileNotFoundError(
            f"Pipeline JAR not found at explicit path: {explicit_path}\n"
            "Check the --jar path, or run `datensee jar download` / `datensee jar build`."
        )

    env_jar = os.environ.get("DATENSEE_JAR")
    if env_jar:
        path = Path(env_jar)
        if path.exists():
            return path
        raise FileNotFoundError(
            f"DATENSEE_JAR points to {env_jar} but the file does not exist.\n"
            "Fix the variable, or run `datensee jar download` / `datensee jar build`."
        )

    candidates = (
        _CACHE_DIR / versioned_jar_filename(),
        _CACHE_DIR / JAR_FILENAME,
        _REPO_JAR,
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise FileNotFoundError(_not_found_message())


def jar_path() -> Path | None:
    """Return the JAR path if found, or None."""
    try:
        return find_jar()
    except FileNotFoundError:
        return None


def ensure_jar() -> Path:
    """Locate the pipeline JAR, downloading the release build if absent.

    Raises:
        FileNotFoundError: No JAR on disk and no release asset for this
            version — the message lists the remaining options.
    """
    try:
        return find_jar()
    except FileNotFoundError:
        pass
    console.print(f"No local pipeline JAR for v{__version__}; fetching the release build.")
    return download_jar()


def _github_token() -> str | None:
    return os.environ.get("DATENSEE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")


def _resolve_private_asset_url(version: str, token: str) -> str:
    """Resolve a pre-signed download URL for the release asset via the GitHub API.

    The direct ``releases/download/`` URL rejects token auth on private
    repositories; the API redirects to a temporary pre-signed URL instead.
    """
    auth = {"Authorization": f"Bearer {token}"}
    release = httpx.get(f"{_GITHUB_API_BASE}/releases/tags/v{version}", headers=auth, timeout=15)
    if release.status_code == 404:
        raise FileNotFoundError(_no_release_message(version))
    release.raise_for_status()

    asset = next((a for a in release.json().get("assets", []) if a["name"] == JAR_FILENAME), None)
    if asset is None:
        raise FileNotFoundError(
            f"GitHub Release v{version} exists but has no {JAR_FILENAME} asset.\n"
            "The release workflow may still be running, or it failed — check "
            f"https://github.com/{GITHUB_REPOSITORY}/releases/tag/v{version}."
        )

    # Don't follow the redirect with the Authorization header attached: the
    # pre-signed URL carries its own signature and rejects a second credential.
    redirect = httpx.get(
        f"{_GITHUB_API_BASE}/releases/assets/{asset['id']}",
        headers={**auth, "Accept": "application/octet-stream"},
        follow_redirects=False,
        timeout=15,
    )
    if not redirect.is_redirect:
        redirect.raise_for_status()
    return redirect.headers["location"]


def _no_release_message(version: str) -> str:
    return (
        f"No prebuilt pipeline JAR for v{version} — checked the GitHub Release "
        f"(https://github.com/{GITHUB_REPOSITORY}/releases/tag/v{version}) and the "
        f"fallback bucket ({_GCS_JAR_URL.format(version=version)}).\n"
        "Options:\n"
        "  --jar <path>            Use a JAR you already have\n"
        "  datensee jar build      Build from a repo checkout (Java 25+ and Gradle)\n"
        "  GITHUB_TOKEN=...        If the repository is private, a token with "
        "read access to releases"
    )


def download_jar(version: str = __version__, *, force: bool = False) -> Path:
    """Download the prebuilt pipeline JAR for ``version``.

    Primary host is the GitHub Release for tag ``v<version>``; when that
    is unreachable (asset missing, or a private repository without a
    token) the public template bucket serves the same JAR from a
    per-version path (``v<version>/datensee-pipeline.jar``) whose
    ``.sha256`` sidecar is verified after download.

    The file lands at ``~/.datensee/jars/datensee-pipeline-<version>.jar``,
    where :func:`find_jar` picks it up.

    Args:
        version: Release version (tag ``v<version>``). Defaults to the
            installed package version.
        force: Re-download even if the cached file exists.

    Returns:
        Path to the downloaded JAR.

    Raises:
        FileNotFoundError: The release or asset does not exist.
        httpx.HTTPError: Network or non-404 HTTP failure.
    """
    destination = _CACHE_DIR / versioned_jar_filename(version)
    if destination.exists() and not force:
        return destination

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    console.print(f"Downloading pipeline JAR v{version}...")

    pinned = _pinned_digest(version)
    try:
        token = _github_token()
        url = (
            _resolve_private_asset_url(version, token)
            if token
            else _GITHUB_RELEASE_ASSET_URL.format(version=version)
        )
        digest = _stream_download(url, destination, not_found=_no_release_message(version))
        _verify_digest(digest, pinned, destination, source=url)
    except FileNotFoundError:
        # The GitHub Release is unreachable (asset missing, or the
        # repository is private and no token is set). Fall back to the
        # public bucket, whose per-version path never changes once
        # published.
        console.print("  GitHub Release unavailable; trying the public bucket fallback.")
        _download_from_bucket(version, destination, pinned)

    console.print(f"  Saved to {destination}")
    return destination


def _pinned_digest(version: str) -> str | None:
    """The wheel-pinned JAR digest, applicable only to the wheel's own version.

    The pin is baked at release time for exactly one artifact; a download
    of any *other* version has no trustworthy local pin and falls back to
    the bucket's best-effort sidecar.
    """
    if version == __version__ and JAR_SHA256:
        return JAR_SHA256.lower()
    return None


def _verify_digest(actual: str, pinned: str | None, destination: Path, *, source: str) -> None:
    """Enforce the wheel-pinned digest on a completed download.

    Raises:
        RuntimeError: The bytes do not match the pin. The file is deleted
            first — a mismatched executable must never remain in the
            search path.
    """
    if pinned is None:
        return
    if actual != pinned:
        destination.unlink(missing_ok=True)
        raise RuntimeError(
            f"Pipeline JAR from {source} does not match the digest pinned in this "
            f"datensee release (expected sha256 {pinned}, got {actual}).\n"
            "The artifact was refused and deleted. This should never happen for a "
            "published release — the host may be compromised or misconfigured. "
            "`datensee jar build` compiles from source instead."
        )
    console.print("  sha256 verified against the release pin.")


def _download_from_bucket(version: str, destination: Path, pinned: str | None) -> None:
    """Fetch the JAR from the public bucket, verifying against the strongest
    available reference: the wheel-pinned digest when this is the wheel's own
    version, else the bucket's best-effort ``.sha256`` sidecar."""
    url = _GCS_JAR_URL.format(version=version)

    expected = pinned
    if expected is None:
        checksum_response = httpx.get(url + ".sha256", timeout=30)
        if checksum_response.status_code == 200:
            expected = checksum_response.text.split()[0].strip().lower()
        elif checksum_response.status_code != 404:
            checksum_response.raise_for_status()

    digest = _stream_download(url, destination, not_found=_no_release_message(version))

    if pinned is not None:
        _verify_digest(digest, pinned, destination, source=url)
        return
    if expected is None:
        console.print(
            "  [yellow]No .sha256 sidecar published for this version; "
            "skipping verification.[/yellow]"
        )
        return
    if digest != expected:
        destination.unlink(missing_ok=True)
        raise RuntimeError(
            f"Checksum mismatch for {url}: expected sha256 {expected}, got {digest}.\n"
            "The bucket copy does not match its published sidecar — refusing the file. "
            "Retry, or use `datensee jar build` / GITHUB_TOKEN for the release asset."
        )
    console.print("  sha256 verified.")


def _stream_download(url: str, destination: Path, *, not_found: str) -> str:
    """Stream ``url`` to ``destination`` with progress; returns the sha256 hex digest.

    Writes go through a ``.tmp`` sibling and are renamed on success, so an
    interrupted download never leaves a truncated JAR in the search path.

    Raises:
        FileNotFoundError: With ``not_found`` as the message, on HTTP 404.
    """
    partial = destination.with_suffix(".tmp")
    digest = hashlib.sha256()
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=300) as response:
            if response.status_code == 404:
                raise FileNotFoundError(not_found)
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0)) or None
            with (
                partial.open("wb") as sink,
                Progress(
                    TextColumn("[bold blue]{task.description}"),
                    BarColumn(),
                    DownloadColumn(),
                    TransferSpeedColumn(),
                    TimeRemainingColumn(),
                    console=console,
                ) as progress,
            ):
                task = progress.add_task("Downloading", total=total)
                for chunk in response.iter_bytes(chunk_size=1 << 16):
                    sink.write(chunk)
                    digest.update(chunk)
                    progress.update(task, advance=len(chunk))
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    partial.replace(destination)
    return digest.hexdigest()


def build_jar() -> Path:
    """Build the pipeline JAR from source via Gradle. Returns the JAR path."""
    pipelines_dir = Path(__file__).parents[3] / "pipelines"

    if not (pipelines_dir / "build.gradle.kts").exists():
        raise FileNotFoundError(
            f"No pipelines/ source tree at {pipelines_dir}.\n"
            "`datensee jar build` needs a source checkout of the datensee repository; "
            "a pip-installed package does not include the Java sources.\n"
            "Use `datensee jar download` to fetch the prebuilt JAR instead."
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
