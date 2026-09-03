"""The pixels check: re-fetch sampled tiles from EE and compare against disk.

Re-fetches sampled non-journaled compute tiles from the EE HV API in NPY
format and compares against pipeline output on disk. In two-tier mode,
on-disk data for a compute tile is read as a window of its output COG.

Opt-in via validate_output(..., pixels=True). Costs EECUs and requires
an EE-enabled GCP project plus rasterio.
"""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from numpy.lib import recfunctions

from datensee.config import PipelineConfig
from datensee.pixel.config import PixelGrid, TileCoordinate
from datensee.pixel.tiling import tile_pixel_grid
from datensee.pixel.validation import CheckResult, CheckStatus
from datensee.pixel.validation.units import (
    TILES_FILE_SKIP_MESSAGE,
    OutputUnit,
    expected_output_units,
    is_m6,
    read_failure_keys,
)

HV_ENDPOINT = (
    "https://earthengine-highvolume.googleapis.com/v1/projects/{project}/image:computePixels"
)

# Float32 tiles from EE should be bit-identical; the epsilon absorbs
# floating-point serialization round-trips (GeoTIFF → read → compare).
_DEFAULT_EPSILON = 1e-4


def sample_evenly[T](items: Sequence[T], n: int) -> list[T]:
    """Deterministic, seed-free sample: first, last, and evenly spaced between."""
    if n <= 0:
        return []
    if len(items) <= n or n == 1:
        return list(items[:n])
    step = (len(items) - 1) / (n - 1)
    return [items[i] for i in sorted({round(i * step) for i in range(n)})]


def _build_hv_request(expression: str, grid: PixelGrid, file_format: str = "NPY") -> dict[str, Any]:
    """Build a computePixels request body for the per-tile grid."""
    t = grid.affine_transform
    return {
        "expression": json.loads(expression),
        "fileFormat": file_format,
        "grid": {
            "dimensions": {"width": grid.dimensions.width, "height": grid.dimensions.height},
            "affineTransform": {
                "scaleX": t.scale_x,
                "shearX": t.shear_x,
                "translateX": t.translate_x,
                "shearY": t.shear_y,
                "scaleY": t.scale_y,
                "translateY": t.translate_y,
            },
            "crsCode": grid.crs_code,
        },
    }


def _fetch_tile_as_numpy(
    client: httpx.Client, project: str, token: str, expression: str, grid: PixelGrid
) -> np.ndarray:
    """Fetch a tile from the EE HV API and decode it as a numpy array."""
    resp = client.post(
        HV_ENDPOINT.format(project=project),
        json=_build_hv_request(expression, grid, "NPY"),
        headers={"Authorization": f"Bearer {token}", "x-goog-user-project": project},
        timeout=120.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"EE HV API returned HTTP {resp.status_code}: {resp.text[:200]}")
    return np.load(io.BytesIO(resp.content))


def _normalize_reference(ref: np.ndarray, disk_shape: tuple[int, ...]) -> np.ndarray:
    """Normalize an EE NPY response to ``(bands, height, width)``.

    Structured ``(H, W)`` arrays (one named field per band) go through
    ``structured_to_unstructured``; plain 2D arrays become single-band;
    plain 3D arrays are matched against ``disk_shape`` in either band
    order. Anything else is returned as-is → the caller reports a
    shape mismatch.
    """
    if ref.dtype.names is not None:
        return np.moveaxis(recfunctions.structured_to_unstructured(ref), -1, 0)
    if ref.ndim == 2:
        return ref[np.newaxis, ...]
    if ref.ndim == 3 and ref.shape != disk_shape:
        band_first = np.moveaxis(ref, -1, 0)
        if band_first.shape == disk_shape:
            return band_first
    return ref


def _read_disk_pixels(
    path: Path, tile: TileCoordinate, unit: OutputUnit, two_tier: bool
) -> np.ndarray:
    """Read all bands of the tile from disk (using a COG window in two-tier mode)."""
    import rasterio

    with rasterio.open(path) as ds:
        if not two_tier:
            return ds.read()
        from rasterio.windows import Window

        return ds.read(
            window=Window(
                tile.col_px - unit.origin_col_px,
                tile.row_px - unit.origin_row_px,
                tile.width_px,
                tile.height_px,
            )
        )


def _compare(disk: np.ndarray, ref_raw: np.ndarray, epsilon: float) -> dict[str, object] | None:
    """Compare one tile's disk data against its reference; None when they agree.

    Only pixels finite on both sides are compared (NaN encodings may
    differ); a tile where nothing is comparable counts as agreement.
    """
    ref = _normalize_reference(ref_raw, disk.shape)
    if disk.shape != ref.shape:
        return {"error": f"Shape mismatch: disk={disk.shape} vs ref={ref.shape}"}
    disk_f, ref_f = disk.astype(np.float64), ref.astype(np.float64)
    valid = np.isfinite(disk_f) & np.isfinite(ref_f)
    if not np.any(valid):
        return None
    abs_diff = np.abs(disk_f[valid] - ref_f[valid])
    max_diff = float(np.max(abs_diff))
    if max_diff <= epsilon:
        return None
    return {
        "max_diff": round(max_diff, 8),
        "mean_diff": round(float(np.mean(abs_diff)), 8),
        "epsilon": epsilon,
    }


def check_pixels(
    output_dir: Path,
    config: PipelineConfig,
    *,
    sample: int = 20,
    gee_project: str,
    access_token: str | None = None,
    epsilon: float = _DEFAULT_EPSILON,
) -> CheckResult:
    """Compare pipeline output pixels against fresh EE HV API fetches.

    Samples up to ``sample`` non-journaled compute tiles deterministically
    (first/last/evenly spaced), re-fetches each with the exact per-tile
    grid the pipeline used, and compares all bands over finite pixels:
    max absolute difference must stay below ``epsilon``.
    """
    units = expected_output_units(config)
    if units is None:
        return CheckResult(
            check_id="pixels", status=CheckStatus.SKIPPED, message=TILES_FILE_SKIP_MESSAGE
        )

    failed_keys = read_failure_keys(output_dir)
    live = [t for t in (config.tile_grid.tiles or []) if (t.row, t.col) not in failed_keys]
    sampled = sample_evenly(live, sample)

    if access_token is None:
        from datensee.auth import get_access_token

        access_token = get_access_token()

    unit_by_key: dict[tuple[int, int], OutputUnit] = {u.key: u for u in units}
    two_tier = is_m6(config)
    parent = config.tile_grid.pixel_grid

    checked = 0
    mismatches: list[dict[str, object]] = []

    with httpx.Client(timeout=120.0) as client:
        for tile in sampled:
            key = (tile.out_row, tile.out_col) if two_tier else (tile.row, tile.col)
            unit = unit_by_key.get(key)
            if unit is None:
                continue
            path = output_dir / unit.filename
            if not path.exists():
                continue

            label = f"{unit.filename}[r{tile.row},c{tile.col}]" if two_tier else unit.filename

            try:
                disk_pixels = _read_disk_pixels(path, tile, unit, two_tier)
            except Exception as exc:
                mismatches.append({"tile": label, "error": f"Failed to read on-disk data: {exc}"})
                continue

            try:
                ref_raw = _fetch_tile_as_numpy(
                    client,
                    gee_project,
                    access_token,
                    config.ee_expression,
                    tile_pixel_grid(parent, tile),
                )
            except Exception as exc:
                mismatches.append({"tile": label, "error": f"Failed to fetch reference: {exc}"})
                continue

            checked += 1
            if (record := _compare(disk_pixels, ref_raw, epsilon)) is not None:
                mismatches.append({"tile": label, **record})

    if checked == 0:
        return CheckResult(
            check_id="pixels",
            status=CheckStatus.SKIPPED,
            message="No tiles could be compared (missing files or API errors)",
            details={"errors": mismatches[:10]} if mismatches else {},
        )
    if not mismatches:
        return CheckResult(
            check_id="pixels",
            status=CheckStatus.PASSED,
            message=f"All {checked} sampled tiles match the EE reference (epsilon={epsilon})",
        )
    return CheckResult(
        check_id="pixels",
        status=CheckStatus.FAILED,
        message=f"{len(mismatches)}/{checked} tiles differ from the EE reference "
        f"(epsilon={epsilon})",
        details={"mismatches": mismatches[:10]},
    )
