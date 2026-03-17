"""Assembly and accounting evals — E05, E08, E10.

E05: VRT references every tile with correct band count/type/dimensions.
E08: tiles_on_disk + tiles_in_failures == tiles_in_config.
E10: Total output size within 0.2x–5x of cost estimator prediction.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from datensee.config import PipelineConfig
from datensee.estimate import estimate_cost
from datensee.eval.catalog import EvalID
from datensee.eval.report import EvalResult, EvalStatus
from datensee.eval.tile_integrity import tile_filename

# Regex to extract (row, col) from tile filenames like tile_r0003_c0012.tif
_TILE_FILENAME_RE = re.compile(r"^tile_r(\d{4})_c(\d{4})\.tif$")


def _tiles_on_disk(output_dir: Path) -> set[tuple[int, int]]:
    """Scan output directory for tile files and return their (row, col) coordinates."""
    found: set[tuple[int, int]] = set()
    for path in output_dir.glob("tile_r*_c*.tif"):
        m = _TILE_FILENAME_RE.match(path.name)
        if m:
            found.add((int(m.group(1)), int(m.group(2))))
    return found


def _tiles_in_failures(output_dir: Path) -> set[tuple[int, int]]:
    """Read failures.json (if present) and return (row, col) of failed tiles."""
    failures_path = output_dir / "failures.json"
    if not failures_path.exists():
        return set()

    try:
        data = json.loads(failures_path.read_text())
    except (json.JSONDecodeError, OSError):
        return set()

    failed: set[tuple[int, int]] = set()
    for entry in data if isinstance(data, list) else data.get("failures", []):
        row = entry.get("row")
        col = entry.get("col")
        if row is not None and col is not None:
            failed.add((int(row), int(col)))
    return failed


def _tiles_in_config(config: PipelineConfig) -> set[tuple[int, int]]:
    """Extract (row, col) set from pipeline config."""
    tiles = config.tile_grid.tiles or []
    return {(t.row, t.col) for t in tiles}


def eval_e08_failure_accounting(
    output_dir: Path,
    config: PipelineConfig,
) -> EvalResult:
    """E08: Verify tiles_on_disk + tiles_in_failures == tiles_in_config.

    Every tile in the config must be accounted for — either as a file on disk
    or as an entry in failures.json. No tiles should be silently lost.
    """
    expected = _tiles_in_config(config)
    on_disk = _tiles_on_disk(output_dir)
    in_failures = _tiles_in_failures(output_dir)

    accounted = on_disk | in_failures
    unaccounted = expected - accounted
    unexpected = accounted - expected

    if not unaccounted and not unexpected:
        return EvalResult(
            eval_id=EvalID.E08,
            status=EvalStatus.PASSED,
            message=(
                f"All {len(expected)} tiles accounted for "
                f"({len(on_disk)} on disk, {len(in_failures)} in failures)"
            ),
        )

    parts: list[str] = []
    if unaccounted:
        parts.append(f"{len(unaccounted)} tiles missing from both disk and failures")
    if unexpected:
        parts.append(f"{len(unexpected)} tiles on disk/failures but not in config")

    return EvalResult(
        eval_id=EvalID.E08,
        status=EvalStatus.FAILED,
        message="; ".join(parts),
        details={
            "unaccounted": sorted(unaccounted)[:20],
            "unexpected": sorted(unexpected)[:20],
            "on_disk": len(on_disk),
            "in_failures": len(in_failures),
            "in_config": len(expected),
        },
    )


# ---------------------------------------------------------------------------
# E05: VRT Completeness
# ---------------------------------------------------------------------------

# Map config data_type to VRT DataType names (mirrors assemble._DATA_TYPE_MAP).
_VRT_DATA_TYPE_MAP: dict[str, str] = {
    "float32": "Float32",
    "float64": "Float64",
    "int16": "Int16",
    "int32": "Int32",
    "uint8": "Byte",
    "uint16": "UInt16",
}


def eval_e05_vrt_completeness(
    output_dir: Path,
    config: PipelineConfig,
) -> EvalResult:
    """E05: Verify mosaic.vrt references every tile with correct metadata.

    Checks:
    - VRT file exists and parses as XML.
    - Every tile in config is referenced as a SimpleSource.
    - Band count matches config.
    - DataType matches config.
    - Raster dimensions match (n_cols * tile_px, n_rows * tile_px).
    """
    vrt_path = output_dir / "mosaic.vrt"
    if not vrt_path.exists():
        return EvalResult(
            eval_id=EvalID.E05,
            status=EvalStatus.FAILED,
            message="mosaic.vrt not found in output directory",
        )

    try:
        tree = ET.parse(vrt_path)
    except ET.ParseError as exc:
        return EvalResult(
            eval_id=EvalID.E05,
            status=EvalStatus.FAILED,
            message=f"mosaic.vrt is not valid XML: {exc}",
        )

    root = tree.getroot()
    issues: list[str] = []

    # Collect all referenced tile filenames from SimpleSource elements
    referenced: set[str] = set()
    for src_filename in root.iter("SourceFilename"):
        if src_filename.text:
            referenced.add(src_filename.text.strip())

    # Check every config tile is referenced
    tiles = config.tile_grid.tiles or []
    expected_names = {tile_filename(t) for t in tiles}
    missing = expected_names - referenced
    if missing:
        issues.append(f"{len(missing)} tiles not referenced in VRT")

    # Band count: count VRTRasterBand elements
    vrt_bands = root.findall("VRTRasterBand")
    expected_bands = config.output.band_count
    if len(vrt_bands) != expected_bands:
        issues.append(f"VRT has {len(vrt_bands)} bands, expected {expected_bands}")

    # DataType
    expected_dtype = _VRT_DATA_TYPE_MAP.get(config.output.data_type, "Float32")
    for band_el in vrt_bands:
        dt = band_el.get("dataType", "")
        if dt != expected_dtype:
            issues.append(f"VRT band dataType '{dt}' != expected '{expected_dtype}'")
            break

    # Raster dimensions
    tile_px = config.tile_grid.tile_size_pixels
    if tiles:
        max_row = max(t.row for t in tiles)
        max_col = max(t.col for t in tiles)
        expected_x = (max_col + 1) * tile_px
        expected_y = (max_row + 1) * tile_px

        vrt_x = int(root.get("rasterXSize", "0"))
        vrt_y = int(root.get("rasterYSize", "0"))
        if vrt_x != expected_x or vrt_y != expected_y:
            issues.append(f"VRT dimensions {vrt_x}x{vrt_y} != expected {expected_x}x{expected_y}")

    if not issues:
        return EvalResult(
            eval_id=EvalID.E05,
            status=EvalStatus.PASSED,
            message=(
                f"VRT references all {len(expected_names)} tiles, "
                f"{expected_bands} band(s), {expected_dtype}"
            ),
        )

    return EvalResult(
        eval_id=EvalID.E05,
        status=EvalStatus.FAILED,
        message="; ".join(issues),
        details={
            "missing_tiles": sorted(missing)[:10] if missing else [],
            "issues": issues,
        },
    )


# ---------------------------------------------------------------------------
# E10: Output Size Plausibility
# ---------------------------------------------------------------------------

_SIZE_LOW_FACTOR = 0.2
_SIZE_HIGH_FACTOR = 5.0


def eval_e10_size_plausibility(
    output_dir: Path,
    config: PipelineConfig,
) -> EvalResult:
    """E10: Verify total output size is plausible vs. cost estimator prediction.

    Actual size should be within 0.2x–5x of the estimated size. Wide bounds
    account for compression variability and nodata regions.
    """
    estimate = estimate_cost(config)
    predicted_bytes = estimate.output_size_bytes

    if predicted_bytes == 0:
        return EvalResult(
            eval_id=EvalID.E10,
            status=EvalStatus.SKIPPED,
            message="Cannot estimate size (tile count unknown)",
        )

    actual_bytes = sum(p.stat().st_size for p in output_dir.glob("tile_r*_c*.tif") if p.is_file())

    low = int(predicted_bytes * _SIZE_LOW_FACTOR)
    high = int(predicted_bytes * _SIZE_HIGH_FACTOR)
    ratio = actual_bytes / predicted_bytes if predicted_bytes > 0 else 0.0

    if low <= actual_bytes <= high:
        return EvalResult(
            eval_id=EvalID.E10,
            status=EvalStatus.PASSED,
            message=(
                f"Output size {_fmt(actual_bytes)} is {ratio:.1f}x "
                f"of predicted {_fmt(predicted_bytes)}"
            ),
            details={
                "actual_bytes": actual_bytes,
                "predicted_bytes": predicted_bytes,
                "ratio": ratio,
            },
        )

    direction = "smaller" if actual_bytes < low else "larger"
    return EvalResult(
        eval_id=EvalID.E10,
        status=EvalStatus.FAILED,
        message=(
            f"Output size {_fmt(actual_bytes)} is {ratio:.1f}x of predicted "
            f"{_fmt(predicted_bytes)} — {direction} than expected "
            f"(bounds: {_SIZE_LOW_FACTOR}x–{_SIZE_HIGH_FACTOR}x)"
        ),
        details={
            "actual_bytes": actual_bytes,
            "predicted_bytes": predicted_bytes,
            "ratio": ratio,
            "bounds": [_SIZE_LOW_FACTOR, _SIZE_HIGH_FACTOR],
        },
    )


def _fmt(n: int) -> str:
    """Human-readable byte size."""
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    if n < 1024**3:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024**3:.2f} GB"
