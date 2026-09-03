"""DatensEE output validation: two checks against pipeline output.

- ``integrity`` (zero-cost, always runs): every expected output unit
  exists (or is fully journaled in ``_failures.json``), has valid TIFF magic
  bytes and non-zero size, and, with rasterio installed, matching dimensions,
  band count, dtype, CRS, and affine origin. Unexpected tile-named files
  are flagged.
- ``pixels`` (opt-in): re-fetches sampled compute tiles from the High
  Volume API and compares every band pixel-for-pixel against on-disk output.

Usage: ``validate_output("./output", config, pixels=True).all_passed``
"""

from __future__ import annotations

import contextlib
import importlib.util
import tempfile
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from rich.panel import Panel
from rich.table import Table

from datensee.config import PipelineConfig
from datensee.pixel.validation.units import (
    FAILURES_FILENAME,
    TILE_FILENAME_RE,
    TILES_FILE_SKIP_MESSAGE,
    OutputUnit,
    expected_output_units,
    read_failure_keys,
    unit_filename,
    unit_keys_on_disk,
)

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

# Truncation heuristic for installs without rasterio. A legitimate
# all-zero 512×512 int16 tile deflates to ~935 B (measured on void SRTM
# tiles), so this threshold sits well below valid compressed tiles.
_MIN_TILE_BYTES = 512
_ORIGIN_TOLERANCE = 1e-6

RASTERIO_MISSING_NOTE = (
    "; metadata checks skipped (rasterio not installed; install datensee[validation])"
)


class CheckStatus(StrEnum):
    """Outcome of a single check."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class CheckResult(BaseModel):
    """Result of running one check."""

    check_id: str
    status: CheckStatus
    message: str = ""
    details: dict[str, Any] = {}


class ValidationReport(BaseModel):
    """Aggregated results from :func:`validate_output`."""

    results: list[CheckResult]
    output_path: str
    config: PipelineConfig

    @property
    def all_passed(self) -> bool:
        """True when no check FAILED or ERRORed (SKIPPED counts as passing)."""
        return all(r.status in (CheckStatus.PASSED, CheckStatus.SKIPPED) for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the report to a JSON-compatible dict."""
        return {
            "output_path": self.output_path,
            "all_passed": self.all_passed,
            "results": [r.model_dump() for r in self.results],
        }

    def render(self) -> Panel:
        """Render a Rich panel summarizing check results."""
        style = {
            CheckStatus.PASSED: "[green]PASS[/green]",
            CheckStatus.FAILED: "[red]FAIL[/red]",
            CheckStatus.SKIPPED: "[dim]SKIP[/dim]",
            CheckStatus.ERROR: "[yellow]ERR[/yellow]",
        }
        table = Table(show_edge=False, pad_edge=False)
        table.add_column("Check", style="bold", min_width=9)
        table.add_column("Status", min_width=8)
        table.add_column("Message")
        for r in self.results:
            table.add_row(r.check_id, style[r.status], r.message)

        n_ok = sum(1 for r in self.results if r.status != CheckStatus.FAILED)
        title = f"Validation results: {n_ok}/{len(self.results)} passed"
        return Panel(table, title=title, border_style="green" if self.all_passed else "red")


DEFAULT_MAX_STAGE_BYTES = 8 * 1024**3
"""Largest gs:// output ``validate`` will mirror locally by default (8 GiB)."""


def _has_rasterio() -> bool:
    return importlib.util.find_spec("rasterio") is not None


def _tiff_magic_ok(path: Path) -> bool:
    """True when the file starts with TIFF magic bytes (``II`` or ``MM``)."""
    try:
        with open(path, "rb") as f:
            return f.read(2) in (b"II", b"MM")
    except OSError:
        return False


def _crs_matches(actual: str, expected: str) -> bool:
    """Compare CRS strings, falling back to pyproj normalization."""
    if actual.strip().upper() == expected.strip().upper():
        return True
    try:
        import pyproj

        return pyproj.CRS.from_user_input(actual) == pyproj.CRS.from_user_input(expected)
    except Exception:
        return False


def _metadata_issues(path: Path, unit: OutputUnit, config: PipelineConfig) -> list[str]:
    """rasterio-backed metadata assertions for one output unit's COG."""
    import rasterio

    issues: list[str] = []
    with rasterio.open(path) as ds:
        if (ds.width, ds.height) != (unit.width_px, unit.height_px):
            issues.append(
                f"dimensions {ds.width}x{ds.height}, expected {unit.width_px}x{unit.height_px}"
            )
        if ds.count != config.output.band_count:
            issues.append(f"bands={ds.count}, expected {config.output.band_count}")
        if ds.dtypes[0] != config.output.data_type:
            issues.append(f"dtype={ds.dtypes[0]}, expected {config.output.data_type}")

        if ds.crs is None:
            issues.append("file has no CRS")
        elif not _crs_matches(str(ds.crs), config.tile_grid.crs):
            issues.append(f"CRS '{ds.crs}' != expected '{config.tile_grid.crs}'")

        # Expected NW-corner origin: parent translate + local origin px × scale
        # (scale_y negative, so row 0 has the largest CRS y).
        p = config.tile_grid.pixel_grid.affine_transform
        expected_x = p.translate_x + unit.origin_col_px * p.scale_x
        expected_y = p.translate_y + unit.origin_row_px * p.scale_y
        if abs(ds.transform.c - expected_x) > _ORIGIN_TOLERANCE:
            issues.append(f"origin X {ds.transform.c} != expected {expected_x}")
        if abs(ds.transform.f - expected_y) > _ORIGIN_TOLERANCE:
            issues.append(f"origin Y {ds.transform.f} != expected {expected_y}")
    return issues


def check_integrity(output_dir: Path, config: PipelineConfig) -> CheckResult:
    """Zero-cost structural check over ALL expected output units.

    Folds together file existence, journal accounting, TIFF magic/size
    plausibility, and (with rasterio) dimension/band/dtype/CRS/origin
    metadata. Metadata reads are cheap, so there is no sampling.
    """
    units = expected_output_units(config)
    if units is None:
        return CheckResult(
            check_id="integrity", status=CheckStatus.SKIPPED, message=TILES_FILE_SKIP_MESSAGE
        )

    failed_keys = read_failure_keys(output_dir)
    with_rasterio = _has_rasterio()

    known_failed: list[str] = []
    problems: list[dict[str, object]] = []
    checked = 0

    for unit in units:
        # A unit whose member compute tiles ALL failed is legitimately
        # absent from disk; a partially-failed unit must still exist.
        if all((m.row, m.col) in failed_keys for m in unit.members):
            known_failed.append(unit.filename)
            continue

        path = output_dir / unit.filename
        if not path.exists():
            problems.append({"unit": unit.filename, "issues": ["file missing"]})
            continue

        checked += 1
        issues: list[str] = []
        if not _tiff_magic_ok(path):
            issues.append("not a TIFF (bad magic bytes)")
        elif with_rasterio:
            # Successfully opening the file verifies file plausibility; a valid
            # COG of an all-zero (ocean/void) tile is under 1 KB.
            try:
                issues.extend(_metadata_issues(path, unit, config))
            except Exception as exc:
                issues.append(f"unreadable: {exc}")
        elif path.stat().st_size < _MIN_TILE_BYTES:
            issues.append(f"implausibly small ({path.stat().st_size} B < {_MIN_TILE_BYTES} B)")
        if issues:
            problems.append({"unit": unit.filename, "issues": issues})

    expected_keys = {u.key for u in units}
    unexpected = [
        unit_filename(r, c) for r, c in sorted(unit_keys_on_disk(output_dir) - expected_keys)
    ]

    details: dict[str, Any] = {
        "problems": problems[:20],
        "unexpected_files": unexpected[:20],
        "known_failed": known_failed[:20],
    }

    if problems or unexpected:
        parts: list[str] = []
        if problems:
            parts.append(
                f"{len(problems)}/{len(units) - len(known_failed)} expected output files "
                "have integrity issues"
            )
        if unexpected:
            parts.append(f"{len(unexpected)} on-disk files match no expected unit")
        return CheckResult(
            check_id="integrity",
            status=CheckStatus.FAILED,
            message="; ".join(parts),
            details=details,
        )

    suffix = f" ({len(known_failed)} known-failed, journaled)" if known_failed else ""
    note = "" if with_rasterio else RASTERIO_MISSING_NOTE
    return CheckResult(
        check_id="integrity",
        status=CheckStatus.PASSED,
        message=f"All {checked} expected output files pass integrity checks{suffix}{note}",
        details=details if known_failed else {},
    )


def validate_output(
    output_path: str | Path,
    config: PipelineConfig,
    *,
    pixels: bool = False,
    sample: int = 20,
    gee_project: str | None = None,
    credentials: Credentials | None = None,
    max_stage_bytes: int = DEFAULT_MAX_STAGE_BYTES,
) -> ValidationReport:
    """Run output checks against pipeline output and return a structured report.

    Args:
        output_path: Directory (or ``gs://`` prefix) containing the exported
            tile GeoTIFFs. A GCS prefix is mirrored into a temporary
            directory first; see :func:`_stage_gcs_output`.
        config: Pipeline config that produced the output.
        pixels: Run the ``pixels`` check (re-fetching reference tiles from
            the High Volume API).
        sample: Maximum compute tiles the ``pixels`` check re-fetches.
        gee_project: GCP project for reference fetches; defaults to the
            config's ``gee_project``.
        credentials: Credentials for reading a ``gs://`` output. ``None``
            uses Application Default Credentials.
        max_stage_bytes: Maximum size of a ``gs://`` output to mirror locally
            before refusing staging.

    Returns:
        ValidationReport with one result per check run. Staging failures
        surface as an ``ERROR`` integrity result, never as an exception.
    """
    uri = str(output_path)

    def _run(check_id: str, runner: Callable[[], CheckResult]) -> CheckResult:
        try:
            return runner()
        except Exception as exc:
            return CheckResult(
                check_id=check_id,
                status=CheckStatus.ERROR,
                message=f"Check raised {type(exc).__name__}: {exc}",
            )

    def _report(results: list[CheckResult]) -> ValidationReport:
        return ValidationReport(results=results, output_path=uri, config=config)

    # Externalized tile lists in GCS are loaded back so checks can run.
    # An unreachable tiles file produces an ERROR result.
    try:
        config = _inline_externalized_tiles(config, credentials)
    except Exception as exc:
        return _report(
            [
                CheckResult(
                    check_id="integrity",
                    status=CheckStatus.ERROR,
                    message=(
                        f"Could not load tile_grid.tiles_file "
                        f"({config.tile_grid.tiles_file}): {type(exc).__name__}: {exc}"
                    ),
                )
            ]
        )

    with contextlib.ExitStack() as stack:
        if uri.startswith("gs://"):
            try:
                staging = _stage_gcs_output(uri, credentials=credentials, max_bytes=max_stage_bytes)
            except Exception as exc:
                return _report(
                    [
                        CheckResult(
                            check_id="integrity",
                            status=CheckStatus.ERROR,
                            message=f"Could not stage {uri}: {type(exc).__name__}: {exc}",
                        )
                    ]
                )
            output = Path(stack.enter_context(staging))
        else:
            output = Path(uri)

        results = [_run("integrity", lambda: check_integrity(output, config))]
        if pixels:
            from datensee.pixel.validation.pixels import check_pixels

            project = gee_project or config.gee_project
            results.append(
                _run(
                    "pixels",
                    lambda: check_pixels(output, config, sample=sample, gee_project=project),
                )
            )
    return _report(results)


def _inline_externalized_tiles(
    config: PipelineConfig, credentials: Credentials | None
) -> PipelineConfig:
    """Resolve ``tile_grid.tiles_file`` into inline tiles for validation.

    Large exports externalize their tile list to ``{output}/_tiles.ndjson``
    (local path or ``gs://``); the checks need the tiles to know which
    output units to expect. Loads and inlines them, clearing ``tiles_file``
    (the model allows exactly one source).

    Args:
        config: Pipeline config, possibly with externalized tiles.
        credentials: For ``gs://`` tile files; ``None`` uses ADC.

    Returns:
        ``config`` unchanged when tiles are already inline; otherwise a
        copy with the loaded tiles.

    Raises:
        FileNotFoundError, ValueError: Unreachable or malformed tile file.
    """
    tiles_file = config.tile_grid.tiles_file
    if config.tile_grid.tiles is not None or not tiles_file:
        return config

    from datensee.pixel.config import TileCoordinate

    if tiles_file.startswith("gs://"):
        from datensee.pixel.retry import _download_gcs_text

        text = _download_gcs_text(tiles_file, credentials)
    else:
        text = Path(tiles_file).read_text(encoding="utf-8")

    tiles = [TileCoordinate.model_validate_json(line) for line in text.splitlines() if line.strip()]
    if not tiles:
        raise ValueError("tiles file is empty")
    grid = config.tile_grid.model_copy(update={"tiles": tiles, "tiles_file": None})
    # `tile_grid` on the envelope is a read-only property over the pixel
    # payload: updating it directly is a silent no-op. Update the payload.
    pixel = config.pixel.model_copy(update={"tile_grid": grid})
    return config.model_copy(update={"pixel": pixel})


def _stage_gcs_output(
    gcs_prefix: str,
    *,
    credentials: Credentials | None,
    max_bytes: int,
    parallelism: int = 8,
) -> tempfile.TemporaryDirectory[str]:
    """Mirror a ``gs://`` output prefix into a temporary directory.

    The checks are written against a local directory (``Path.exists``,
    ``glob``, ``rasterio.open``); rather than teach every helper about
    GCS, we stage what they read (the output COGs and the failures
    journal) and run the local code unchanged. Only the prefix's own
    objects are copied (no nested "directories"); the listing is one
    request, the downloads run in parallel.

    Args:
        gcs_prefix: ``gs://bucket/prefix`` of the export.
        credentials: Credentials for the storage client (``None`` = ADC).
        max_bytes: Refuse (``ValueError``) when the COGs exceed this size.
        parallelism: Concurrent downloads.

    Returns:
        The staging directory; the caller owns its lifetime. On any
        failure the partially filled directory is removed before the
        exception propagates.
    """
    from concurrent.futures import ThreadPoolExecutor

    from datensee.auth import gcs_client, split_gcs_uri

    bucket_name, prefix = split_gcs_uri(gcs_prefix)
    listing_prefix = f"{prefix.rstrip('/')}/" if prefix.strip("/") else ""

    client = gcs_client(credentials)
    wanted = [
        blob
        for blob in client.list_blobs(bucket_name, prefix=listing_prefix, delimiter="/")
        if (name := blob.name[len(listing_prefix) :])
        and (TILE_FILENAME_RE.match(name) or name == FAILURES_FILENAME)
    ]
    total = sum(blob.size or 0 for blob in wanted)
    if total > max_bytes:
        raise ValueError(
            f"{len(wanted)} files / {total / 1e9:.1f} GB under {gcs_prefix}; validate mirrors "
            f"a gs:// output locally and refuses above {max_bytes / 1e9:.0f} GB. Run it on a "
            "host with the disk for it (max_stage_bytes=…), or validate the local run."
        )

    staging = tempfile.TemporaryDirectory(prefix="datensee-validate-")
    root = Path(staging.name)
    try:
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            for _ in pool.map(
                lambda blob: blob.download_to_filename(
                    str(root / blob.name[len(listing_prefix) :])
                ),
                wanted,
            ):
                pass
    except BaseException:
        staging.cleanup()
        raise
    return staging
