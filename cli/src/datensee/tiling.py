"""Region → tile grid decomposition.

Converts an EE region (GeoJSON polygon) into a regular grid of tiles
at the requested scale and projection. This is a pure geometry operation —
no EE API calls required.

The export normalizes to a canonical :class:`PixelGrid` (CRS code + 6-tuple
affine transform + integer dimensions). Tiles inside the export are integer
pixel rectangles within that grid; CRS bboxes are derived deterministically
from ``transform × pixel_offsets`` and never persisted.

Grid alignment: the parent grid's ``translate_x``/``translate_y`` are snapped
outward to whole tile boundaries against a global ``(0, 0)`` origin. Two
exports sharing CRS, scale, and tile size therefore produce identical
pixel-to-CRS transforms regardless of region — cross-export grid alignment
is unconditional. For geographic CRSs we use a single equator constant
(``111_320 m/°``) to convert ``scale_meters`` to degrees, with no latitude
correction; ``scale_meters`` becomes a nominal label, not a metric guarantee.
A user who needs strict on-the-ground pixel widths supplies a projected CRS.
"""

from __future__ import annotations

import math
from typing import Any

import pyproj
from shapely.geometry import Polygon, shape
from shapely.ops import transform

from datensee.config import (
    AffineTransform,
    GridDimensions,
    PixelGrid,
    TileCoordinate,
    TileGrid,
)

# Meters per degree of longitude / latitude at the equator on the WGS84
# ellipsoid. Used as a single constant for geographic CRSs — see module
# docstring for the rationale.
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
    """Return pixel size in native CRS units.

    Projected CRSs are assumed to use meters; ``scale_meters`` passes through
    unchanged. Geographic CRSs convert via the equator constant
    (``111_320 m/°``) — no latitude correction. The on-the-ground pixel
    width is then ``scale_meters`` only at the equator and shrinks with
    ``cos(lat)`` toward the poles. This is the explicit "scale at origin"
    choice that makes cross-export grid alignment unconditional; users who
    need true metric pixels supply a projected CRS.
    """
    crs_obj = pyproj.CRS.from_user_input(crs)
    if crs_obj.is_geographic:
        return scale_meters / _METERS_PER_DEGREE_EQUATOR
    return scale_meters


def tile_pixel_grid(parent: PixelGrid, tile: TileCoordinate) -> PixelGrid:
    """Per-tile :class:`PixelGrid` derived by translating the parent transform.

    The result is what gets sent verbatim to EE's ``computePixels`` for this
    tile — same axis convention, same CRS, just shifted to the tile's NW
    corner and resized to the tile's pixel dimensions.
    """
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
    """Return the tile's CRS bbox ``(x_min, y_min, x_max, y_max)``.

    Derived deterministically from ``transform × pixel_offsets`` — never
    persisted on the tile itself. The math assumes axis-aligned grids
    (``shear_x == shear_y == 0``), which is always the case for our
    decomposition.
    """
    p = parent.affine_transform
    x_min = p.translate_x + tile.col_px * p.scale_x
    x_max = x_min + tile.width_px * p.scale_x
    # scale_y is negative (NW-corner origin convention); the larger CRS y
    # corresponds to the smaller row_px.
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

    The grid is aligned to a global origin at ``(0, 0)`` in the target CRS:
    parent-grid corners snap outward to whole tile boundaries against that
    anchor, so two exports sharing CRS + scale + tile size always share the
    same pixel-to-CRS transform. Edge tiles are full-size (never clipped);
    the EE expression should be clipped to the region so out-of-bounds
    pixels are nodata rather than computed.

    M6 two-tier tiling: when ``output_tile_size_pixels`` is set (must be a
    positive multiple of ``tile_size_pixels``), each compute tile is also
    stamped with ``out_row``/``out_col`` — the indices of the *output* tile
    it belongs to. The output grid snaps to the same global origin so output
    tiles align with compute tiles. When unset, ``out_row``/``out_col``
    mirror ``row``/``col`` (one COG per compute tile).

    Args:
        geojson_geometry: GeoJSON geometry dict (polygon or multipolygon) in WGS84.
        scale_meters: Pixel size in meters. For geographic CRSs converted via
            the equator constant (no latitude correction); for projected CRSs
            passed through unchanged.
        crs: Target CRS for the tile grid (EPSG code or proj string).
        tile_size_pixels: Compute tile edge in pixels.
        output_tile_size_pixels: Output COG edge in pixels (multiple of
            ``tile_size_pixels``). When None, one COG per compute tile.

    Returns:
        :class:`TileGrid` covering the bounding box of the input geometry.
    """
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

    # Snap outward to whole tile boundaries.
    col_start_tile_px = (col_start_px // tile_size_pixels) * tile_size_pixels
    col_end_tile_px = math.ceil(col_end_px / tile_size_pixels) * tile_size_pixels
    row_start_tile_px = (row_start_px // tile_size_pixels) * tile_size_pixels
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

    n_per_output = output_tile_size_pixels // tile_size_pixels if output_tile_size_pixels else 1

    # Output-tile localization: same logic as compute, snap-and-floor to
    # produce consecutive small indices per export.
    out_col_start = col_start_tile_px // tile_size_pixels // n_per_output
    out_row_start = row_start_tile_px // tile_size_pixels // n_per_output

    tiles: list[TileCoordinate] = []
    for row_offset_px in range(0, height_px, tile_size_pixels):
        for col_offset_px in range(0, width_px, tile_size_pixels):
            # Bbox derived from the parent transform — used only to test
            # intersection with the region; not stored on the tile.
            tile_xmin = parent_grid.affine_transform.translate_x + (
                col_offset_px * parent_grid.affine_transform.scale_x
            )
            tile_ymax = parent_grid.affine_transform.translate_y + (
                row_offset_px * parent_grid.affine_transform.scale_y
            )
            tile_xmax = tile_xmin + tile_size_pixels * parent_grid.affine_transform.scale_x
            tile_ymin = tile_ymax + tile_size_pixels * parent_grid.affine_transform.scale_y

            tile_box = Polygon.from_bounds(tile_xmin, tile_ymin, tile_xmax, tile_ymax)
            if not geom_native.intersects(tile_box):
                continue

            local_row = row_offset_px // tile_size_pixels
            local_col = col_offset_px // tile_size_pixels
            # Absolute compute-tile index within the global tile grid →
            # absolute output-tile index → localize back into export-space.
            absolute_compute_col = col_start_tile_px // tile_size_pixels + local_col
            absolute_compute_row = row_start_tile_px // tile_size_pixels + local_row
            out_col = absolute_compute_col // n_per_output - out_col_start
            out_row = absolute_compute_row // n_per_output - out_row_start

            tiles.append(
                TileCoordinate(
                    col_px=col_offset_px,
                    row_px=row_offset_px,
                    width_px=tile_size_pixels,
                    height_px=tile_size_pixels,
                    row=local_row,
                    col=local_col,
                    out_row=out_row,
                    out_col=out_col,
                )
            )

    return TileGrid(
        pixel_grid=parent_grid,
        tile_size_pixels=tile_size_pixels,
        tiles=tiles,
    )
