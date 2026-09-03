"""Region to tile grid decomposition.

Cuts an Earth Engine region (GeoJSON geometry) into a regular tile grid
at the requested scale and projection without calling the EE API.

Normalizes exports to a canonical PixelGrid (CRS, 6-element affine transform,
and dimensions in pixels). Output tiles are integer pixel rectangles within
that grid; CRS bounding boxes are derived as needed.

Grid alignment: origin translation snaps outward to whole tile boundaries
against (0, 0) in the target CRS. Exports sharing CRS, scale, and tile size
produce matching pixel-to-CRS alignments regardless of region bounds. For
geographic CRSs, scale converts to degrees at the equator (111,320 m/deg)
without latitude adjustment. Use a projected CRS if conformal metric pixels
are required.
"""

from __future__ import annotations

import math
from typing import Any

import pyproj
from shapely.geometry import Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from datensee.pixel.config import (
    AffineTransform,
    GridDimensions,
    PixelGrid,
    TileCoordinate,
    TileGrid,
)

# Nominal meters per degree at the equator for WGS84.
_METERS_PER_DEGREE_EQUATOR: float = 111_320.0


def _reproject_geometry(
    geom: Any,
    src_crs: str,
    dst_crs: str,
) -> Any:
    """Reproject a Shapely geometry between two CRS strings."""
    transformer = pyproj.Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    return transform(transformer.transform, geom)


def _pixel_size_native(crs: str, scale_meters: float) -> float:
    """Return pixel size in native CRS units."""
    if scale_meters <= 0:
        raise ValueError(f"scale_meters ({scale_meters}) must be positive")
    crs_obj = pyproj.CRS.from_user_input(crs)
    if crs_obj.is_geographic:
        return scale_meters / _METERS_PER_DEGREE_EQUATOR
    return scale_meters


def tile_pixel_grid(parent: PixelGrid, tile: TileCoordinate) -> PixelGrid:
    """Per-tile PixelGrid shifted to the tile's NW corner."""
    p = parent.affine_transform
    return PixelGrid(
        crs_code=parent.crs_code,
        affine_transform=AffineTransform(
            scale_x=p.scale_x,
            shear_x=p.shear_x,
            translate_x=p.translate_x + tile.col_px * p.scale_x + tile.row_px * p.shear_x,
            shear_y=p.shear_y,
            scale_y=p.scale_y,
            translate_y=p.translate_y + tile.col_px * p.shear_y + tile.row_px * p.scale_y,
        ),
        dimensions=GridDimensions(width=tile.width_px, height=tile.height_px),
    )


def tile_bbox(parent: PixelGrid, tile: TileCoordinate) -> tuple[float, float, float, float]:
    """Return the tile's CRS bbox (x_min, y_min, x_max, y_max).

    Derived from transform * pixel offsets assuming axis-aligned grids.
    """
    p = parent.affine_transform
    x_min = p.translate_x + tile.col_px * p.scale_x
    x_max = x_min + tile.width_px * p.scale_x
    # scale_y is negative (NW origin): larger CRS y corresponds to smaller row_px.
    y_max = p.translate_y + tile.row_px * p.scale_y
    y_min = y_max + tile.height_px * p.scale_y
    return x_min, y_min, x_max, y_max


def decompose_region(
    geojson_geometry: dict[str, Any],
    scale_meters: float,
    crs: str = "EPSG:4326",
    tile_size_pixels: int = 512,
    output_tile_size_pixels: int | None = None,
) -> TileGrid:
    """Decompose a GeoJSON geometry into a snapped, regular tile grid.

    The grid is aligned to a global origin at (0, 0) in the target CRS:
    parent-grid corners snap outward to whole tile boundaries against that
    anchor, so exports sharing CRS, scale, and tile size share the same
    pixel-to-CRS transform. Edge tiles are full-size (never clipped).

    In two-tier mode (when output_tile_size_pixels is set), the origin snaps
    to output-tile boundaries and each compute tile records (out_row, out_col)
    calculated as (row_px // out, col_px // out).
    """
    if tile_size_pixels <= 0:
        raise ValueError(f"tile_size_pixels ({tile_size_pixels}) must be positive")
    if output_tile_size_pixels is not None and output_tile_size_pixels % tile_size_pixels != 0:
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
    pixel_size = _pixel_size_native(crs, scale_meters)

    # Region bbox in pixel offsets against global (0, 0). Rows count
    # downward from the origin (scale_y < 0), so the NW corner has the
    # largest CRS y but the smallest row_px.
    col_start_px = math.floor(minx / pixel_size)
    col_end_px = math.ceil(maxx / pixel_size)
    row_start_px = math.floor(-maxy / pixel_size)
    row_end_px = math.ceil(-miny / pixel_size)

    # Snap outward to whole tile boundaries. In two-tier mode the snap unit is
    # the *output* tile size: the Java assembler recovers each output
    # tile's origin from local pixel offsets (`(col_px // out) * out`),
    # which is only correct when the parent origin itself sits on an
    # output-tile boundary. Output size is a multiple of compute size,
    # so compute-tile alignment (and cross-export grid alignment) is
    # preserved.
    snap_px = output_tile_size_pixels or tile_size_pixels
    col_start_tile_px = (col_start_px // snap_px) * snap_px
    col_end_tile_px = math.ceil(col_end_px / tile_size_pixels) * tile_size_pixels
    row_start_tile_px = (row_start_px // snap_px) * snap_px
    row_end_tile_px = math.ceil(row_end_px / tile_size_pixels) * tile_size_pixels

    width_px = col_end_tile_px - col_start_tile_px
    height_px = row_end_tile_px - row_start_tile_px

    parent_grid = PixelGrid(
        crs_code=crs,
        affine_transform=AffineTransform(
            scale_x=pixel_size,
            shear_x=0.0,
            translate_x=col_start_tile_px * pixel_size,
            shear_y=0.0,
            scale_y=-pixel_size,
            translate_y=-row_start_tile_px * pixel_size,
        ),
        dimensions=GridDimensions(width=width_px, height=height_px),
    )

    return tiles_for_grid(
        parent_grid,
        tile_size_pixels=tile_size_pixels,
        output_tile_size_pixels=output_tile_size_pixels,
        geom_native=geom_native,
    )


def decompose_pixel_grid(
    pixel_grid: PixelGrid,
    *,
    tile_size_pixels: int = 512,
    output_tile_size_pixels: int | None = None,
    geojson_geometry: dict[str, Any] | None = None,
) -> TileGrid:
    """Tile an exact, caller-supplied PixelGrid verbatim.

    The CRS, affine transform, and dimensions are used directly as given.
    Dimensions must be an exact multiple of tile_size_pixels on both axes
    (and of output_tile_size_pixels in two-tier mode).
    """
    geom_native: BaseGeometry | None = None
    if geojson_geometry is not None:
        geom_wgs84 = shape(geojson_geometry)
        geom_native = (
            geom_wgs84
            if pixel_grid.crs_code == "EPSG:4326"
            else _reproject_geometry(geom_wgs84, "EPSG:4326", pixel_grid.crs_code)
        )
    return tiles_for_grid(
        pixel_grid,
        tile_size_pixels=tile_size_pixels,
        output_tile_size_pixels=output_tile_size_pixels,
        geom_native=geom_native,
    )


def _tile_coordinate_at(
    row_px: int,
    col_px: int,
    tile_size_px: int,
    output_tile_size_px: int,
) -> TileCoordinate:
    return TileCoordinate(
        col_px=col_px,
        row_px=row_px,
        width_px=tile_size_px,
        height_px=tile_size_px,
        row=row_px // tile_size_px,
        col=col_px // tile_size_px,
        out_row=row_px // output_tile_size_px,
        out_col=col_px // output_tile_size_px,
    )


def _tile_intersects(
    geom: BaseGeometry,
    transform: AffineTransform,
    row_px: int,
    col_px: int,
    tile_size_px: int,
) -> bool:
    tile_xmin = transform.translate_x + col_px * transform.scale_x
    tile_ymax = transform.translate_y + row_px * transform.scale_y
    tile_xmax = tile_xmin + tile_size_px * transform.scale_x
    tile_ymin = tile_ymax + tile_size_px * transform.scale_y
    return bool(geom.intersects(Polygon.from_bounds(tile_xmin, tile_ymin, tile_xmax, tile_ymax)))


def tiles_for_grid(
    parent_grid: PixelGrid,
    *,
    tile_size_pixels: int,
    output_tile_size_pixels: int | None = None,
    geom_native: BaseGeometry | None = None,
) -> TileGrid:
    """Cut a parent PixelGrid into a regular tile grid.

    Tiles are full-size integer pixel rectangles. When geom_native is provided,
    tiles not intersecting the geometry are excluded.
    """
    if tile_size_pixels <= 0:
        raise ValueError(f"tile_size_pixels ({tile_size_pixels}) must be positive")
    if output_tile_size_pixels is not None and output_tile_size_pixels <= 0:
        raise ValueError(f"output_tile_size_pixels ({output_tile_size_pixels}) must be positive")

    width_px = parent_grid.dimensions.width
    height_px = parent_grid.dimensions.height
    if width_px % tile_size_pixels or height_px % tile_size_pixels:
        raise ValueError(
            f"grid dimensions {width_px}x{height_px} are not a whole multiple of "
            f"tile_size_pixels ({tile_size_pixels}); partial edge tiles are not "
            "supported. Pad the grid to a tile-size multiple, or change --tile-size."
        )
    if output_tile_size_pixels is not None and output_tile_size_pixels % tile_size_pixels != 0:
        raise ValueError(
            f"output_tile_size_pixels ({output_tile_size_pixels}) must be a multiple "
            f"of tile_size_pixels ({tile_size_pixels})"
        )

    p = parent_grid.affine_transform
    out_px = output_tile_size_pixels or tile_size_pixels

    offsets = (
        (r, c)
        for r in range(0, height_px, tile_size_pixels)
        for c in range(0, width_px, tile_size_pixels)
    )
    tiles = [
        _tile_coordinate_at(r, c, tile_size_pixels, out_px)
        for r, c in offsets
        if geom_native is None or _tile_intersects(geom_native, p, r, c, tile_size_pixels)
    ]

    if not tiles:
        raise ValueError(
            "no tiles were produced: the supplied region does not intersect the "
            "target grid. Check the region and CRS, or omit the region to tile the "
            "whole grid."
        )
    return TileGrid(
        pixel_grid=parent_grid,
        tile_size_pixels=tile_size_pixels,
        tiles=tiles,
    )
