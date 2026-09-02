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
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from datensee.pixel.config import (
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

    two-tier tiling: when ``output_tile_size_pixels`` is set (must be a
    positive multiple of ``tile_size_pixels``), the parent grid origin snaps
    outward to *output*-tile boundaries (a superset of compute-tile
    boundaries, so cross-export alignment is unaffected) and each compute
    tile is stamped with ``out_row``/``out_col`` — pure local arithmetic
    ``(row_px // out, col_px // out)``. The Java assembler derives every
    output tile's origin from the same arithmetic, so the two sides agree
    by construction. When unset, ``out_row``/``out_col`` mirror
    ``row``/``col`` (one COG per compute tile).

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
    """Tile an exact, caller-supplied :class:`PixelGrid` verbatim.

    Unlike :func:`decompose_region`, the CRS, affine transform, and
    dimensions are taken exactly as given — no ``scale``→pixel-size
    derivation, no outward snapping to a global origin, no equator
    constant. Two exports that pass the *same* ``pixel_grid`` therefore
    address pixel-for-pixel identical grids, so a per-pixel diff between
    them (or against an existing asset on that grid) is exact rather than
    subject to reprojected-bbox tessellation.

    This mirrors Earth Engine's own ``Export.image({crs, crsTransform,
    dimensions})`` parameters. The grid's ``dimensions`` must be a whole
    multiple of ``tile_size_pixels`` on both axes (and of
    ``output_tile_size_pixels`` in two-tier mode) — partial edge tiles are
    not supported; :func:`tiles_for_grid` raises otherwise.

    Args:
        pixel_grid: The exact output grid (CRS + affine + dimensions).
        tile_size_pixels: Compute tile edge in pixels.
        output_tile_size_pixels: Output COG edge (multiple of tile size);
            ``None`` for one COG per compute tile.
        geojson_geometry: Optional WGS84 geometry; when given, tiles that
            do not intersect it are skipped (their pixels would be nodata).
            When ``None``, every tile of the grid is emitted.

    Returns:
        :class:`TileGrid` over the given grid.
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


def tiles_for_grid(
    parent_grid: PixelGrid,
    *,
    tile_size_pixels: int,
    output_tile_size_pixels: int | None = None,
    geom_native: BaseGeometry | None = None,
) -> TileGrid:
    """Cut a parent :class:`PixelGrid` into a regular tile grid.

    Shared by :func:`decompose_region` (parent derived from region+scale)
    and :func:`decompose_pixel_grid` (parent given verbatim). Tiles are
    full-size integer pixel rectangles; ``geom_native`` (in the grid's
    own CRS) is an optional intersection filter — ``None`` keeps every
    tile.

    Raises:
        ValueError: The grid dimensions are not a whole multiple of the
            tile size (partial edge tiles are unsupported), or the output
            tile size is not a multiple of the compute tile size / does
            not evenly divide the grid.
    """
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
    # Note: the grid need NOT be a whole multiple of output_tile_size_pixels.
    # A partial final output tile is legal — the assembler zero-fills the
    # missing compute-tile blocks (out indices are floor(local_px / OTS)).

    p = parent_grid.affine_transform
    out_px = output_tile_size_pixels or tile_size_pixels

    tiles: list[TileCoordinate] = []
    for row_offset_px in range(0, height_px, tile_size_pixels):
        for col_offset_px in range(0, width_px, tile_size_pixels):
            if geom_native is not None:
                # Bbox from transform × pixel offsets — only for the
                # intersection test; never stored on the tile.
                tile_xmin = p.translate_x + col_offset_px * p.scale_x
                tile_ymax = p.translate_y + row_offset_px * p.scale_y
                tile_xmax = tile_xmin + tile_size_pixels * p.scale_x
                tile_ymin = tile_ymax + tile_size_pixels * p.scale_y
                if not geom_native.intersects(
                    Polygon.from_bounds(tile_xmin, tile_ymin, tile_xmax, tile_ymax)
                ):
                    continue

            tiles.append(
                TileCoordinate(
                    col_px=col_offset_px,
                    row_px=row_offset_px,
                    width_px=tile_size_pixels,
                    height_px=tile_size_pixels,
                    row=row_offset_px // tile_size_pixels,
                    col=col_offset_px // tile_size_pixels,
                    out_row=row_offset_px // out_px,
                    out_col=col_offset_px // out_px,
                )
            )

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
