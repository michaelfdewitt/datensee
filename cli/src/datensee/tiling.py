"""Region → tile grid decomposition.

Converts an EE region (GeoJSON polygon) into a regular grid of tiles
at the requested scale and projection. This is a pure geometry operation —
no EE API calls required.

Grid alignment: the tile grid is snapped to a global origin (0, 0) in the
target CRS so that tiles from independent exports at the same scale and
tile size are always pixel-aligned. Edge tiles extend to full tile size
(never shrunk) but the EE expression should be clipped to the export
region so that out-of-bounds pixels are nodata rather than computed.
"""

from __future__ import annotations

import math
from typing import Any

import pyproj
from shapely.geometry import Polygon, shape
from shapely.ops import transform

from datensee.config import TileCoordinate, TileGrid


def _reproject_geometry(
    geom: Any,
    src_crs: str,
    dst_crs: str,
) -> Any:
    """Reproject a Shapely geometry between two CRS strings."""
    transformer = pyproj.Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    return transform(transformer.transform, geom)


def _pixel_size_native(crs: str, scale_meters: float) -> float:
    """Return the pixel size in native CRS units.

    For geographic CRS (degrees), approximate degrees-per-meter at the equator.
    For projected CRS, units are assumed to be meters.
    """
    crs_obj = pyproj.CRS.from_user_input(crs)
    if crs_obj.is_geographic:
        deg_per_meter = 1.0 / 111_320.0
        return scale_meters * deg_per_meter
    return scale_meters


def decompose_region(
    geojson_geometry: dict[str, Any],
    scale_meters: float,
    crs: str = "EPSG:4326",
    tile_size_pixels: int = 512,
    output_tile_size_pixels: int | None = None,
) -> TileGrid:
    """Decompose a GeoJSON geometry into a snapped, regular tile grid.

    The grid is aligned to a global origin at (0, 0) in the target CRS.
    This means that for the same CRS, scale, and tile_size_pixels, two
    independent calls will produce pixel-aligned grids regardless of the
    input region. Edge tiles are full-size (never clipped).

    M6 two-tier tiling: when ``output_tile_size_pixels`` is set (must be a
    positive multiple of ``tile_size_pixels``), each compute tile is also
    stamped with ``out_row``/``out_col`` — the indices of the *output* tile
    it belongs to. The output grid is snapped to the same global origin
    so output tiles align with compute tiles. When unset, ``out_row`` and
    ``out_col`` mirror ``row`` and ``col`` (one COG per compute tile).

    Args:
        geojson_geometry: GeoJSON geometry dict (polygon or multipolygon), in WGS84.
        scale_meters: Pixel size in meters. Tiles will be tile_size_pixels × scale wide.
        crs: Target CRS for the tile grid (EPSG code or proj string).
        tile_size_pixels: Compute tile edge in pixels.
        output_tile_size_pixels: Output COG edge in pixels (multiple of
            tile_size_pixels). When None, one COG per compute tile.

    Returns:
        TileGrid covering the bounding box of the input geometry.
    """
    if (
        output_tile_size_pixels is not None
        and output_tile_size_pixels % tile_size_pixels != 0
    ):
        raise ValueError(
            f"output_tile_size_pixels ({output_tile_size_pixels}) must be a multiple "
            f"of tile_size_pixels ({tile_size_pixels})"
        )

    geom_wgs84 = shape(geojson_geometry)

    if crs == "EPSG:4326":
        geom_native = geom_wgs84
    else:
        geom_native = _reproject_geometry(geom_wgs84, "EPSG:4326", crs)

    minx, miny, maxx, maxy = geom_native.bounds

    pixel_native = _pixel_size_native(crs, scale_meters)
    tile_size_native = pixel_native * tile_size_pixels

    # Tile indices and counts — ceil ensures we cover the full bbox.
    # Grid origin is implicitly (0, 0) in the target CRS: tile boundaries
    # are aligned to col * tile_size_native, row * tile_size_native.
    col_start = math.floor(minx / tile_size_native)
    row_start = math.floor(miny / tile_size_native)
    col_end = math.ceil(maxx / tile_size_native)
    row_end = math.ceil(maxy / tile_size_native)

    # Output-tile mapping (M6). Both grids snap to the same global origin
    # at (0, 0), so an absolute compute-grid index can be quotiented by N
    # to get its absolute output-grid index. Localizing to the bbox uses
    # the floor of the bbox-start indices so all compute tiles inside one
    # output tile share the same out_row/out_col.
    n_per_output = (
        output_tile_size_pixels // tile_size_pixels
        if output_tile_size_pixels
        else 1
    )
    out_row_start = row_start // n_per_output
    out_col_start = col_start // n_per_output

    tiles: list[TileCoordinate] = []
    for row_idx in range(row_start, row_end):
        for col_idx in range(col_start, col_end):
            tile_xmin = col_idx * tile_size_native
            tile_ymin = row_idx * tile_size_native
            tile_xmax = tile_xmin + tile_size_native
            tile_ymax = tile_ymin + tile_size_native

            # Skip tiles that don't intersect the actual geometry.
            tile_box = Polygon.from_bounds(tile_xmin, tile_ymin, tile_xmax, tile_ymax)
            if not geom_native.intersects(tile_box):
                continue

            local_row = row_idx - row_start
            local_col = col_idx - col_start
            tiles.append(
                TileCoordinate(
                    x_min=tile_xmin,
                    y_min=tile_ymin,
                    x_max=tile_xmax,
                    y_max=tile_ymax,
                    row=local_row,
                    col=local_col,
                    out_row=(row_idx // n_per_output) - out_row_start,
                    out_col=(col_idx // n_per_output) - out_col_start,
                )
            )

    return TileGrid(
        crs=crs,
        scale_meters=scale_meters,
        tile_size_pixels=tile_size_pixels,
        tiles=tiles,
    )
