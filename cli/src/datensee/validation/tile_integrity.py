"""Tile file integrity checks — E01, E02, E09.

E01: Every expected tile exists, has TIFF magic bytes, >1 KB.
E02: Tile pixel dimensions, band count, and data type match config.
E09: Sampled pixel values fall within expected range for the data type.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from datensee.config import PipelineConfig, TileCoordinate
from datensee.validation.catalog import CheckID
from datensee.validation.report import CheckResult, CheckStatus
from datensee.validation.tiff import read_tiff_info, read_tiff_pixels, validate_tiff_magic

_MIN_TILE_BYTES = 1024  # 1 KB


def tile_filename(tile: TileCoordinate) -> str:
    """Canonical tile filename: tile_r0003_c0012.tif."""
    return f"tile_r{tile.row:04d}_c{tile.col:04d}.tif"


def check_e01_tile_file_integrity(
    output_dir: Path,
    tiles: list[TileCoordinate],
) -> CheckResult:
    """E01: Verify every expected tile exists as a valid TIFF >1 KB.

    Runs on ALL tiles (cheap — just stat + 2-byte read per file).
    """
    missing: list[str] = []
    bad_magic: list[str] = []
    too_small: list[str] = []

    for tile in tiles:
        name = tile_filename(tile)
        path = output_dir / name

        if not path.exists():
            missing.append(name)
            continue

        if not validate_tiff_magic(path):
            bad_magic.append(name)
            continue

        if path.stat().st_size < _MIN_TILE_BYTES:
            too_small.append(name)

    total_issues = len(missing) + len(bad_magic) + len(too_small)

    if total_issues == 0:
        return CheckResult(
            check_id=CheckID.E01,
            status=CheckStatus.PASSED,
            message=f"All {len(tiles)} tiles are valid TIFF files",
        )

    parts: list[str] = []
    if missing:
        parts.append(f"{len(missing)} missing")
    if bad_magic:
        parts.append(f"{len(bad_magic)} invalid TIFF")
    if too_small:
        parts.append(f"{len(too_small)} too small (<1 KB)")

    return CheckResult(
        check_id=CheckID.E01,
        status=CheckStatus.FAILED,
        message=f"{total_issues}/{len(tiles)} tiles failed: {', '.join(parts)}",
        details={
            "missing": missing[:10],
            "bad_magic": bad_magic[:10],
            "too_small": too_small[:10],
        },
    )


# Map config data_type to numpy/rasterio dtype strings.
_DTYPE_MAP: dict[str, set[str]] = {
    "float32": {"float32"},
    "float64": {"float64"},
    "int16": {"int16"},
    "int32": {"int32"},
    "uint8": {"uint8"},
    "uint16": {"uint16"},
}


def check_e02_tile_dimensions(
    output_dir: Path,
    config: PipelineConfig,
    sampled_tiles: list[TileCoordinate],
) -> CheckResult:
    """E02: Verify tile dimensions, band count, and data type match config.

    Runs on sampled tiles (requires rasterio for metadata reading).
    """
    expected_size = config.tile_grid.tile_size_pixels
    expected_bands = config.output.band_count
    expected_dtypes = _DTYPE_MAP.get(config.output.data_type, {config.output.data_type})

    failures: list[dict[str, object]] = []
    checked = 0

    for tile in sampled_tiles:
        path = output_dir / tile_filename(tile)
        if not path.exists():
            continue  # E01 catches missing files

        try:
            info = read_tiff_info(path)
        except Exception as exc:
            failures.append({"tile": tile_filename(tile), "error": str(exc)})
            continue

        checked += 1
        issues: list[str] = []

        if info.width != expected_size or info.height != expected_size:
            issues.append(
                f"dimensions {info.width}x{info.height}, expected {expected_size}x{expected_size}"
            )

        if info.band_count != expected_bands:
            issues.append(f"bands={info.band_count}, expected {expected_bands}")

        if info.dtype not in expected_dtypes:
            issues.append(f"dtype={info.dtype}, expected {config.output.data_type}")

        if issues:
            failures.append({"tile": tile_filename(tile), "issues": issues})

    if not failures:
        return CheckResult(
            check_id=CheckID.E02,
            status=CheckStatus.PASSED,
            message=(
                f"All {checked} sampled tiles match config "
                f"({expected_size}x{expected_size}, "
                f"{expected_bands} band(s), {config.output.data_type})"
            ),
        )

    return CheckResult(
        check_id=CheckID.E02,
        status=CheckStatus.FAILED,
        message=f"{len(failures)}/{checked} sampled tiles have wrong dimensions/dtype/bands",
        details={"failures": failures[:10]},
    )


# ---------------------------------------------------------------------------
# E09: Pixel Range Sanity
# ---------------------------------------------------------------------------

# Expected finite-value ranges per data type.
# These are generous bounds — if pixels fall outside these, something is very wrong.
_DTYPE_RANGES: dict[str, tuple[float, float]] = {
    "float32": (-1e10, 1e10),
    "float64": (-1e15, 1e15),
    "int16": (-32768, 32767),
    "int32": (-2_147_483_648, 2_147_483_647),
    "uint8": (0, 255),
    "uint16": (0, 65535),
}

# Maximum fraction of tiles that can be all-NaN before we fail.
_MAX_ALL_NAN_FRACTION = 0.05


def check_e09_pixel_range_sanity(
    output_dir: Path,
    config: PipelineConfig,
    sampled_tiles: list[TileCoordinate],
) -> CheckResult:
    """E09: Verify sampled pixel values fall within expected range.

    For each sampled tile:
    - Read band 1 pixels.
    - Check that finite (non-NaN) values are within the data type's range.
    - Flag tiles where 100% of pixels are NaN (suspicious if >5% of sample).

    Passes if >95% of finite pixels are in range and <5% tiles are all-NaN.
    """
    data_type = config.output.data_type
    value_range = _DTYPE_RANGES.get(data_type, (-1e10, 1e10))
    lo, hi = value_range

    checked = 0
    all_nan_tiles: list[str] = []
    out_of_range_tiles: list[dict[str, object]] = []
    total_finite = 0
    total_in_range = 0

    for tile in sampled_tiles:
        path = output_dir / tile_filename(tile)
        if not path.exists():
            continue

        try:
            pixels = read_tiff_pixels(path, band=1)
        except Exception:
            continue

        checked += 1
        flat = pixels.flatten().astype(np.float64)
        finite_mask = np.isfinite(flat)
        n_finite = int(np.sum(finite_mask))

        if n_finite == 0:
            all_nan_tiles.append(tile_filename(tile))
            continue

        finite_vals = flat[finite_mask]
        in_range = int(np.sum((finite_vals >= lo) & (finite_vals <= hi)))
        total_finite += n_finite
        total_in_range += in_range

        if in_range < n_finite:
            out_of_range = n_finite - in_range
            out_of_range_tiles.append(
                {
                    "tile": tile_filename(tile),
                    "out_of_range": out_of_range,
                    "total_finite": n_finite,
                    "min": float(np.min(finite_vals)),
                    "max": float(np.max(finite_vals)),
                }
            )

    if checked == 0:
        return CheckResult(
            check_id=CheckID.E09,
            status=CheckStatus.SKIPPED,
            message="No tiles could be read for pixel range check",
        )

    in_range_frac = total_in_range / total_finite if total_finite > 0 else 1.0
    nan_frac = len(all_nan_tiles) / checked if checked > 0 else 0.0

    issues: list[str] = []
    if in_range_frac < 0.95:
        issues.append(
            f"Only {in_range_frac:.1%} of finite pixels in range [{lo}, {hi}] (expected >95%)"
        )
    if nan_frac > _MAX_ALL_NAN_FRACTION:
        issues.append(
            f"{len(all_nan_tiles)}/{checked} tiles are all-NaN ({nan_frac:.0%}, expected <5%)"
        )

    if not issues:
        return CheckResult(
            check_id=CheckID.E09,
            status=CheckStatus.PASSED,
            message=(
                f"{checked} tiles checked: {in_range_frac:.0%} of pixels in range, "
                f"{len(all_nan_tiles)} all-NaN tiles"
            ),
        )

    return CheckResult(
        check_id=CheckID.E09,
        status=CheckStatus.FAILED,
        message="; ".join(issues),
        details={
            "in_range_fraction": round(in_range_frac, 4),
            "all_nan_tiles": all_nan_tiles[:10],
            "out_of_range_tiles": out_of_range_tiles[:10],
        },
    )
