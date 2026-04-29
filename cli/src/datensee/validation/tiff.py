"""Minimal GeoTIFF reader — rasterio wrapper with graceful fallback.

If rasterio is installed (via `datensee[validation]`), it provides CRS, affine,
dimensions, band count, dtype, and pixel data. Without rasterio, basic
TIFF validation (magic bytes, file size) still works.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class TiffInfo:
    """Metadata extracted from a GeoTIFF file."""

    width: int
    height: int
    band_count: int
    dtype: str
    crs: str | None
    transform: tuple[float, ...] | None  # (a, b, c, d, e, f) affine


def _has_rasterio() -> bool:
    try:
        import rasterio  # noqa: F401

        return True
    except ImportError:
        return False


def read_tiff_info(path: Path) -> TiffInfo:
    """Read metadata from a GeoTIFF file.

    Uses rasterio if available, otherwise raises ImportError with guidance.
    """
    if not _has_rasterio():
        raise ImportError(
            "rasterio is required for GeoTIFF metadata reading. "
            "Install it with: pip install datensee[validation]"
        )

    import rasterio

    with rasterio.open(path) as ds:
        t = ds.transform
        return TiffInfo(
            width=ds.width,
            height=ds.height,
            band_count=ds.count,
            dtype=ds.dtypes[0],
            crs=str(ds.crs) if ds.crs else None,
            transform=(t.a, t.b, t.c, t.d, t.e, t.f),
        )


def read_tiff_pixels(path: Path, band: int = 1) -> np.ndarray:
    """Read pixel data from a GeoTIFF band.

    Args:
        path: Path to the GeoTIFF file.
        band: 1-indexed band number.

    Returns:
        2D numpy array of pixel values.
    """
    if not _has_rasterio():
        raise ImportError(
            "rasterio is required for pixel reading. "
            "Install it with: pip install datensee[validation]"
        )

    import rasterio

    with rasterio.open(path) as ds:
        return ds.read(band)


def validate_tiff_magic(path: Path) -> bool:
    """Check if a file starts with valid TIFF magic bytes (II or MM)."""
    try:
        with open(path, "rb") as f:
            magic = f.read(2)
        return magic in (b"II", b"MM")
    except OSError:
        return False
