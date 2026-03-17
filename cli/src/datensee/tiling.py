"""Region → tile grid decomposition.

Converts an EE region (GeoJSON polygon) into a regular grid of tiles
at the requested scale and projection. This is a pure geometry operation —
no EE API calls required.
"""

from __future__ import annotations

import math
from typing import Any

import pyproj
from shapely.geometry import Polygon, mapping, shape
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


def decompose_region(
    geojson_geometry: dict[str, Any],
    scale_meters: float,
    crs: str = "EPSG:4326",
    tile_size_pixels: int = 512,
) -> TileGrid:
    """Decompose a GeoJSON geometry into a regular tile grid.

    Args:
        geojson_geometry: GeoJSON geometry dict (polygon or multipolygon), in WGS84.
        scale_meters: Pixel size in meters. Tiles will be tile_size_pixels × scale_meters wide.
        crs: Target CRS for the tile grid (EPSG code or proj string).
        tile_size_pixels: Number of pixels per tile edge.

    Returns:
        TileGrid covering the bounding box of the input geometry.
    """
    geom_wgs84 = shape(geojson_geometry)

    if crs == "EPSG:4326":
        geom_native = geom_wgs84
    else:
        geom_native = _reproject_geometry(geom_wgs84, "EPSG:4326", crs)

    minx, miny, maxx, maxy = geom_native.bounds

    # Tile edge length in native CRS units.
    # For geographic CRS (degrees), approximate degrees-per-meter at equator.
    # For projected CRS, units are typically meters.
    crs_obj = pyproj.CRS.from_user_input(crs)
    if crs_obj.is_geographic:
        # degrees → meters: ~111,320 m per degree at equator
        deg_per_meter = 1.0 / 111_320.0
        tile_size_native = scale_meters * tile_size_pixels * deg_per_meter
    else:
        tile_size_native = scale_meters * tile_size_pixels

    n_cols = math.ceil((maxx - minx) / tile_size_native)
    n_rows = math.ceil((maxy - miny) / tile_size_native)

    tiles: list[TileCoordinate] = []
    for row in range(n_rows):
        for col in range(n_cols):
            tile_xmin = minx + col * tile_size_native
            tile_ymin = miny + row * tile_size_native
            tile_xmax = min(tile_xmin + tile_size_native, maxx)
            tile_ymax = min(tile_ymin + tile_size_native, maxy)

            # Skip tiles that don't intersect the actual geometry.
            tile_box = Polygon.from_bounds(tile_xmin, tile_ymin, tile_xmax, tile_ymax)
            if not geom_native.intersects(tile_box):
                continue

            tiles.append(
                TileCoordinate(
                    x_min=tile_xmin,
                    y_min=tile_ymin,
                    x_max=tile_xmax,
                    y_max=tile_ymax,
                    row=row,
                    col=col,
                )
            )

    return TileGrid(crs=crs, scale_meters=scale_meters, tile_size_pixels=tile_size_pixels, tiles=tiles)
