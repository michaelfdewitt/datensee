"""Spatial checks — E03, E04, E06.

E03: CRS and affine transform match config tile coordinates.
E04: Adjacent tiles' shared edge pixels form smooth continuation.
E06: VRT bounding box covers the export region.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from datensee.config import PipelineConfig, TileCoordinate
from datensee.validation.catalog import CheckID
from datensee.validation.report import CheckResult, CheckStatus
from datensee.validation.tiff import read_tiff_info, read_tiff_pixels
from datensee.validation.tile_integrity import tile_filename

# E03 tolerance for affine origin comparison (in CRS units).
_ORIGIN_TOLERANCE = 1e-6


def check_e03_tile_geospatial_metadata(
    output_dir: Path,
    config: PipelineConfig,
    sampled_tiles: list[TileCoordinate],
) -> CheckResult:
    """E03: Verify CRS matches config and affine origin matches tile coordinates.

    For each sampled tile, the GeoTIFF's CRS must match the config CRS,
    and the affine transform origin (translateX, translateY) must correspond
    to (tile.x_min, tile.y_max) — top-left corner in geographic convention.
    """
    expected_crs = config.tile_grid.crs
    failures: list[dict[str, object]] = []
    checked = 0

    for tile in sampled_tiles:
        path = output_dir / tile_filename(tile)
        if not path.exists():
            continue

        try:
            info = read_tiff_info(path)
        except Exception as exc:
            failures.append({"tile": tile_filename(tile), "error": str(exc)})
            continue

        checked += 1
        issues: list[str] = []

        # CRS check — normalize both to compare
        if info.crs is not None:
            if not _crs_matches(info.crs, expected_crs):
                issues.append(f"CRS '{info.crs}' != expected '{expected_crs}'")

        # Affine origin check: transform = (scaleX, shearX, translateX, shearY, scaleY, translateY)
        # translateX should be tile.x_min, translateY should be tile.y_max
        if info.transform is not None:
            tx = info.transform[2]  # translateX = origin X = x_min
            ty = info.transform[5]  # translateY = origin Y = y_max
            if abs(tx - tile.x_min) > _ORIGIN_TOLERANCE:
                issues.append(f"origin X {tx} != tile x_min {tile.x_min}")
            if abs(ty - tile.y_max) > _ORIGIN_TOLERANCE:
                issues.append(f"origin Y {ty} != tile y_max {tile.y_max}")

        if issues:
            failures.append({"tile": tile_filename(tile), "issues": issues})

    if not failures:
        return CheckResult(
            check_id=CheckID.E03,
            status=CheckStatus.PASSED,
            message=f"All {checked} sampled tiles have correct CRS and affine origin",
        )

    return CheckResult(
        check_id=CheckID.E03,
        status=CheckStatus.FAILED,
        message=f"{len(failures)}/{checked} sampled tiles have metadata issues",
        details={"failures": failures[:10]},
    )


def _crs_matches(actual: str, expected: str) -> bool:
    """Compare CRS strings, normalizing common variations."""
    # Try direct string match first
    if actual == expected:
        return True

    # Normalize: strip, upper, handle EPSG variants
    a = actual.strip().upper()
    e = expected.strip().upper()
    if a == e:
        return True

    # pyproj-based comparison for robust matching
    try:
        import pyproj

        crs_a = pyproj.CRS.from_user_input(actual)
        crs_e = pyproj.CRS.from_user_input(expected)
        return crs_a == crs_e
    except Exception:
        return False


# ---------------------------------------------------------------------------
# E04: Boundary Continuity
# ---------------------------------------------------------------------------

# Default threshold for mean absolute difference at tile boundaries.
# EE tiles of natural imagery typically have gradual spatial variation;
# a large discontinuity at a tile edge indicates misalignment.
_BOUNDARY_THRESHOLD = 50.0


def check_e04_boundary_continuity(
    output_dir: Path,
    config: PipelineConfig,
    sampled_tiles: list[TileCoordinate],
    *,
    threshold: float = _BOUNDARY_THRESHOLD,
) -> CheckResult:
    """E04: Verify adjacent tiles' shared edge pixels form a smooth continuation.

    For sampled tiles, find right and top neighbors in the tile grid. Read the
    shared edge (last column of left tile vs first column of right tile, etc.)
    and compare with mean absolute difference.
    """
    tiles = config.tile_grid.tiles or []
    by_pos: dict[tuple[int, int], TileCoordinate] = {(t.row, t.col): t for t in tiles}

    pairs_checked = 0
    discontinuities: list[dict[str, object]] = []

    for tile in sampled_tiles:
        # Check right neighbor
        right = by_pos.get((tile.row, tile.col + 1))
        if right:
            result = _check_edge(output_dir, tile, right, edge="vertical", config=config)
            if result is not None:
                pairs_checked += 1
                if result["mad"] > threshold:
                    discontinuities.append(result)

        # Check top neighbor (row + 1 = north in our grid convention)
        top = by_pos.get((tile.row + 1, tile.col))
        if top:
            result = _check_edge(output_dir, tile, top, edge="horizontal", config=config)
            if result is not None:
                pairs_checked += 1
                if result["mad"] > threshold:
                    discontinuities.append(result)

    if pairs_checked == 0:
        return CheckResult(
            check_id=CheckID.E04,
            status=CheckStatus.SKIPPED,
            message="No adjacent tile pairs found in sample",
        )

    if not discontinuities:
        return CheckResult(
            check_id=CheckID.E04,
            status=CheckStatus.PASSED,
            message=(
                f"All {pairs_checked} tile boundary pairs are continuous (threshold={threshold})"
            ),
        )

    return CheckResult(
        check_id=CheckID.E04,
        status=CheckStatus.FAILED,
        message=(
            f"{len(discontinuities)}/{pairs_checked} tile boundaries exceed threshold={threshold}"
        ),
        details={"discontinuities": discontinuities[:10]},
    )


def _check_edge(
    output_dir: Path,
    tile_a: TileCoordinate,
    tile_b: TileCoordinate,
    edge: str,
    config: PipelineConfig,
) -> dict[str, object] | None:
    """Compare shared edge pixels between two adjacent tiles.

    Returns dict with mean absolute difference, or None if files can't be read.
    """
    path_a = output_dir / tile_filename(tile_a)
    path_b = output_dir / tile_filename(tile_b)

    if not path_a.exists() or not path_b.exists():
        return None

    try:
        pixels_a = read_tiff_pixels(path_a, band=1)
        pixels_b = read_tiff_pixels(path_b, band=1)
    except Exception:
        return None

    if edge == "vertical":
        # Right edge of A vs left edge of B
        edge_a = pixels_a[:, -1].astype(np.float64)
        edge_b = pixels_b[:, 0].astype(np.float64)
    else:
        # Top edge of A vs bottom edge of B
        # Our grid: row increases northward; in raster space, row 0 is top.
        # tile_a is the southern tile, tile_b is the northern tile.
        # Southern tile's top row (raster row 0) vs northern tile's bottom row.
        edge_a = pixels_a[0, :].astype(np.float64)
        edge_b = pixels_b[-1, :].astype(np.float64)

    # Mask NaN values
    valid = ~(np.isnan(edge_a) | np.isnan(edge_b))
    if not np.any(valid):
        return None

    mad = float(np.mean(np.abs(edge_a[valid] - edge_b[valid])))

    return {
        "tile_a": tile_filename(tile_a),
        "tile_b": tile_filename(tile_b),
        "edge": edge,
        "mad": round(mad, 4),
    }


# ---------------------------------------------------------------------------
# E06: VRT Spatial Correctness
# ---------------------------------------------------------------------------


def check_e06_vrt_spatial_correctness(
    output_dir: Path,
    config: PipelineConfig,
) -> CheckResult:
    """E06: Verify VRT bounding box covers the tile grid extent.

    The VRT's geo-extent (from GeoTransform + raster dimensions) must cover
    the full extent of the tile grid defined in the config.
    """
    import xml.etree.ElementTree as ET

    vrt_path = output_dir / "mosaic.vrt"
    if not vrt_path.exists():
        return CheckResult(
            check_id=CheckID.E06,
            status=CheckStatus.FAILED,
            message="mosaic.vrt not found",
        )

    try:
        tree = ET.parse(vrt_path)
    except ET.ParseError as exc:
        return CheckResult(
            check_id=CheckID.E06,
            status=CheckStatus.FAILED,
            message=f"VRT parse error: {exc}",
        )

    root = tree.getroot()

    # Parse GeoTransform: "x_origin, pixel_w, 0, y_origin, 0, -pixel_h"
    gt_el = root.find("GeoTransform")
    if gt_el is None or gt_el.text is None:
        return CheckResult(
            check_id=CheckID.E06,
            status=CheckStatus.FAILED,
            message="VRT missing GeoTransform element",
        )

    gt = [float(v.strip()) for v in gt_el.text.split(",")]
    x_origin, pixel_w, _, y_origin, _, neg_pixel_h = gt

    raster_x = int(root.get("rasterXSize", "0"))
    raster_y = int(root.get("rasterYSize", "0"))

    vrt_x_min = x_origin
    vrt_x_max = x_origin + raster_x * pixel_w
    vrt_y_max = y_origin
    vrt_y_min = y_origin + raster_y * neg_pixel_h  # neg_pixel_h is negative

    # Expected extent from config
    tiles = config.tile_grid.tiles or []
    if not tiles:
        return CheckResult(
            check_id=CheckID.E06,
            status=CheckStatus.SKIPPED,
            message="No tiles in config to compare against",
        )

    grid_x_min = min(t.x_min for t in tiles)
    grid_x_max = max(t.x_max for t in tiles)
    grid_y_min = min(t.y_min for t in tiles)
    grid_y_max = max(t.y_max for t in tiles)

    tol = abs(pixel_w) * 0.5  # half-pixel tolerance

    issues: list[str] = []
    if vrt_x_min > grid_x_min + tol:
        issues.append(f"VRT x_min {vrt_x_min} > grid x_min {grid_x_min}")
    if vrt_x_max < grid_x_max - tol:
        issues.append(f"VRT x_max {vrt_x_max} < grid x_max {grid_x_max}")
    if vrt_y_min > grid_y_min + tol:
        issues.append(f"VRT y_min {vrt_y_min} > grid y_min {grid_y_min}")
    if vrt_y_max < grid_y_max - tol:
        issues.append(f"VRT y_max {vrt_y_max} < grid y_max {grid_y_max}")

    if not issues:
        return CheckResult(
            check_id=CheckID.E06,
            status=CheckStatus.PASSED,
            message="VRT bounding box covers the full tile grid extent",
            details={
                "vrt_bbox": [vrt_x_min, vrt_y_min, vrt_x_max, vrt_y_max],
                "grid_bbox": [grid_x_min, grid_y_min, grid_x_max, grid_y_max],
            },
        )

    return CheckResult(
        check_id=CheckID.E06,
        status=CheckStatus.FAILED,
        message=f"VRT bbox does not cover grid: {'; '.join(issues)}",
        details={
            "vrt_bbox": [vrt_x_min, vrt_y_min, vrt_x_max, vrt_y_max],
            "grid_bbox": [grid_x_min, grid_y_min, grid_x_max, grid_y_max],
            "issues": issues,
        },
    )
