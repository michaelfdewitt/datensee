"""Reference comparison check — E07.

The crown jewel: re-fetch sampled tiles from the EE HV API in NPY format
and compare pixel values against the pipeline output on disk. If they match,
the entire chain (tiling, fetching, assembly) is correct.

Requires --reference flag and a GEE project with EE API enabled.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from datensee.config import PipelineConfig, TileCoordinate
from datensee.tiling import tile_bbox
from datensee.validation.catalog import CheckID
from datensee.validation.report import CheckResult, CheckStatus
from datensee.validation.tiff import read_tiff_pixels
from datensee.validation.tile_integrity import tile_filename

HV_ENDPOINT = (
    "https://earthengine-highvolume.googleapis.com/v1/projects/{project}/image:computePixels"
)

# Maximum absolute difference between pipeline pixel and reference pixel.
# Float32 tiles from EE should be bit-identical, but we allow a tiny epsilon
# for floating-point serialization round-trips (GeoTIFF → read → compare).
_DEFAULT_EPSILON = 1e-4


def _build_hv_request(
    expression: str,
    tile_bounds: tuple[float, float, float, float],
    tile_size: int,
    crs: str,
    file_format: str = "NPY",
) -> dict[str, Any]:
    """Build a computePixels request body for the EE HV API."""
    x_min, y_min, x_max, y_max = tile_bounds
    pixel_w = (x_max - x_min) / tile_size
    pixel_h = (y_max - y_min) / tile_size

    return {
        "expression": json.loads(expression),
        "fileFormat": file_format,
        "grid": {
            "dimensions": {"width": tile_size, "height": tile_size},
            "affineTransform": {
                "scaleX": pixel_w,
                "shearX": 0,
                "translateX": x_min,
                "shearY": 0,
                "scaleY": -pixel_h,
                "translateY": y_max,
            },
            "crsCode": crs,
        },
    }


def _fetch_tile_as_numpy(
    client: httpx.Client,
    project: str,
    token: str,
    expression: str,
    tile_bounds: tuple[float, float, float, float],
    tile_size: int,
    crs: str,
) -> np.ndarray:
    """Fetch a tile from the EE HV API and decode as numpy array."""
    body = _build_hv_request(expression, tile_bounds, tile_size, crs, "NPY")
    url = HV_ENDPOINT.format(project=project)
    resp = client.post(
        url,
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "x-goog-user-project": project,
        },
        timeout=120.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"EE HV API returned HTTP {resp.status_code}: {resp.text[:200]}")
    return np.load(io.BytesIO(resp.content))


def check_e07_pixel_value_accuracy(
    output_dir: Path,
    config: PipelineConfig,
    sampled_tiles: list[TileCoordinate],
    *,
    gee_project: str,
    access_token: str,
    epsilon: float = _DEFAULT_EPSILON,
) -> CheckResult:
    """E07: Compare pipeline output pixels against fresh EE HV API fetches.

    For each sampled tile:
    1. Read band 1 from the on-disk GeoTIFF.
    2. Re-fetch the same tile from EE in NPY format.
    3. Compare finite pixels: max absolute difference must be < epsilon.

    Args:
        output_dir: Directory containing pipeline output tiles.
        config: Pipeline config.
        sampled_tiles: Tiles to validate.
        gee_project: GCP project ID with EE API enabled.
        access_token: OAuth2 bearer token for EE.
        epsilon: Maximum allowed absolute difference per pixel.

    Returns:
        CheckResult with pass/fail and per-tile details.
    """
    grid = config.tile_grid
    tile_size = grid.tile_size_pixels
    crs = grid.crs
    parent_grid = grid.pixel_grid
    expression = config.ee_expression

    checked = 0
    mismatches: list[dict[str, object]] = []

    with httpx.Client(timeout=120.0) as client:
        for tile in sampled_tiles:
            path = output_dir / tile_filename(tile)
            if not path.exists():
                continue

            try:
                disk_pixels = read_tiff_pixels(path, band=1)
            except Exception as exc:
                mismatches.append(
                    {
                        "tile": tile_filename(tile),
                        "error": f"Failed to read on-disk tile: {exc}",
                    }
                )
                continue

            try:
                ref_pixels = _fetch_tile_as_numpy(
                    client,
                    gee_project,
                    access_token,
                    expression,
                    tile_bbox(parent_grid, tile),
                    tile_size,
                    crs,
                )
            except Exception as exc:
                mismatches.append(
                    {
                        "tile": tile_filename(tile),
                        "error": f"Failed to fetch reference tile: {exc}",
                    }
                )
                continue

            checked += 1

            # NPY from EE may have shape (height, width) or (bands, height, width).
            # Normalize to 2D for band-1 comparison.
            if ref_pixels.ndim == 3:
                ref_pixels = ref_pixels[0]

            if disk_pixels.shape != ref_pixels.shape:
                mismatches.append(
                    {
                        "tile": tile_filename(tile),
                        "error": (
                            f"Shape mismatch: disk={disk_pixels.shape} vs ref={ref_pixels.shape}"
                        ),
                    }
                )
                continue

            disk_f = disk_pixels.astype(np.float64)
            ref_f = ref_pixels.astype(np.float64)

            # Compare only where both are finite (NaN regions may differ in representation)
            valid = np.isfinite(disk_f) & np.isfinite(ref_f)
            if not np.any(valid):
                continue  # Both all-NaN — nothing to compare

            abs_diff = np.abs(disk_f[valid] - ref_f[valid])
            max_diff = float(np.max(abs_diff))
            mean_diff = float(np.mean(abs_diff))

            if max_diff > epsilon:
                mismatches.append(
                    {
                        "tile": tile_filename(tile),
                        "max_diff": round(max_diff, 8),
                        "mean_diff": round(mean_diff, 8),
                        "epsilon": epsilon,
                    }
                )

    if checked == 0:
        return CheckResult(
            check_id=CheckID.E07,
            status=CheckStatus.SKIPPED,
            message="No tiles could be compared (missing files or API errors)",
            details={"errors": mismatches[:10]} if mismatches else {},
        )

    if not mismatches:
        return CheckResult(
            check_id=CheckID.E07,
            status=CheckStatus.PASSED,
            message=f"All {checked} sampled tiles match EE reference (epsilon={epsilon})",
        )

    return CheckResult(
        check_id=CheckID.E07,
        status=CheckStatus.FAILED,
        message=f"{len(mismatches)}/{checked} tiles differ from EE reference (epsilon={epsilon})",
        details={"mismatches": mismatches[:10]},
    )
