"""Tests for region → tile grid decomposition (M11 PixelGrid shape)."""

from __future__ import annotations

import math

import pytest

from datensee.config import (
    AffineTransform,
    GridDimensions,
    PixelGrid,
    TileCoordinate,
)
from datensee.tiling import (
    _METERS_PER_DEGREE_EQUATOR,
    _pixel_size_native,
    decompose_region,
    tile_bbox,
    tile_pixel_grid,
)

CALIFORNIA_BBOX = {
    "type": "Polygon",
    "coordinates": [
        [
            [-124.5, 32.5],
            [-114.0, 32.5],
            [-114.0, 42.0],
            [-124.5, 42.0],
            [-124.5, 32.5],
        ]
    ],
}

SMALL_SQUARE = {
    "type": "Polygon",
    "coordinates": [
        [
            [0.0, 0.0],
            [0.1, 0.0],
            [0.1, 0.1],
            [0.0, 0.1],
            [0.0, 0.0],
        ]
    ],
}

SF_BAY = {
    "type": "Polygon",
    "coordinates": [
        [
            [-122.5, 37.75],
            [-122.25, 37.75],
            [-122.25, 38.0],
            [-122.5, 38.0],
            [-122.5, 37.75],
        ]
    ],
}


# ---------------------------------------------------------------------------
# Basic decomposition
# ---------------------------------------------------------------------------


def test_decompose_returns_at_least_one_tile() -> None:
    grid = decompose_region(SMALL_SQUARE, scale_meters=30.0)
    assert len(grid.tiles) >= 1


def test_decompose_tiles_are_positive_pixel_rectangles() -> None:
    grid = decompose_region(SMALL_SQUARE, scale_meters=30.0)
    for tile in grid.tiles:
        assert tile.width_px > 0
        assert tile.height_px > 0
        assert tile.col_px >= 0
        assert tile.row_px >= 0


def test_decompose_crs_is_preserved() -> None:
    grid = decompose_region(SMALL_SQUARE, scale_meters=30.0, crs="EPSG:4326")
    assert grid.crs == "EPSG:4326"
    assert grid.pixel_grid.crs_code == "EPSG:4326"


def test_decompose_pixel_size_is_consistent_with_scale() -> None:
    grid = decompose_region(SMALL_SQUARE, scale_meters=100.0, crs="EPSG:4326")
    expected = 100.0 / _METERS_PER_DEGREE_EQUATOR
    assert grid.pixel_size == pytest.approx(expected)
    assert grid.pixel_grid.affine_transform.scale_x == pytest.approx(expected)
    assert grid.pixel_grid.affine_transform.scale_y == pytest.approx(-expected)


def test_decompose_pixel_size_for_projected_crs_is_meters() -> None:
    grid = decompose_region(SF_BAY, scale_meters=10.0, crs="EPSG:32610", tile_size_pixels=256)
    assert grid.pixel_size == pytest.approx(10.0)


def test_large_scale_produces_fewer_tiles() -> None:
    grid_fine = decompose_region(CALIFORNIA_BBOX, scale_meters=30.0)
    grid_coarse = decompose_region(CALIFORNIA_BBOX, scale_meters=1000.0)
    assert len(grid_fine.tiles) > len(grid_coarse.tiles)


def test_tile_row_col_are_non_negative() -> None:
    grid = decompose_region(SMALL_SQUARE, scale_meters=30.0)
    for tile in grid.tiles:
        assert tile.row >= 0
        assert tile.col >= 0


def test_decompose_projected_crs_epsg32610() -> None:
    grid = decompose_region(SF_BAY, scale_meters=10.0, crs="EPSG:32610", tile_size_pixels=256)
    assert grid.crs == "EPSG:32610"
    assert len(grid.tiles) >= 1
    # UTM Zone 10N coordinates for SF Bay are ~5e5 easting, ~4.2e6 northing —
    # the parent grid should sit there, well above degree magnitudes.
    assert grid.pixel_grid.affine_transform.translate_x > 1000
    assert grid.pixel_grid.affine_transform.translate_y > 1000


def test_decompose_projected_crs_produces_different_translate() -> None:
    grid_geo = decompose_region(SF_BAY, scale_meters=30.0, crs="EPSG:4326")
    grid_utm = decompose_region(SF_BAY, scale_meters=30.0, crs="EPSG:32610")
    assert (
        grid_geo.pixel_grid.affine_transform.translate_x
        != grid_utm.pixel_grid.affine_transform.translate_x
    )


# ---------------------------------------------------------------------------
# Helpers: tile_pixel_grid, tile_bbox
# ---------------------------------------------------------------------------


def _parent_grid(
    *,
    scale: float = 1.0,
    translate_x: float = 0.0,
    translate_y: float = 0.0,
    width: int = 100,
    height: int = 100,
    crs: str = "EPSG:4326",
) -> PixelGrid:
    return PixelGrid(
        crs_code=crs,
        affine_transform=AffineTransform(
            scale_x=scale,
            shear_x=0.0,
            translate_x=translate_x,
            shear_y=0.0,
            scale_y=-scale,
            translate_y=translate_y,
        ),
        dimensions=GridDimensions(width=width, height=height),
    )


class TestTilePixelGrid:
    def test_root_tile_at_origin_inherits_parent_origin(self) -> None:
        parent = _parent_grid(scale=2.0, translate_x=10.0, translate_y=40.0)
        tile = TileCoordinate(col_px=0, row_px=0, width_px=10, height_px=10)
        sub = tile_pixel_grid(parent, tile)
        assert sub.affine_transform.translate_x == 10.0
        assert sub.affine_transform.translate_y == 40.0
        assert sub.dimensions.width == 10
        assert sub.dimensions.height == 10
        assert sub.affine_transform.scale_x == 2.0
        assert sub.affine_transform.scale_y == -2.0

    def test_offset_tile_translates_origin(self) -> None:
        parent = _parent_grid(scale=2.0, translate_x=10.0, translate_y=40.0)
        # 5 px right, 7 px down
        tile = TileCoordinate(col_px=5, row_px=7, width_px=4, height_px=4)
        sub = tile_pixel_grid(parent, tile)
        assert sub.affine_transform.translate_x == 10.0 + 5 * 2.0  # 20
        assert sub.affine_transform.translate_y == 40.0 + 7 * (-2.0)  # 26
        assert sub.dimensions.width == 4
        assert sub.dimensions.height == 4

    def test_crs_propagates(self) -> None:
        parent = _parent_grid(crs="EPSG:32610")
        tile = TileCoordinate(col_px=0, row_px=0, width_px=8, height_px=8)
        assert tile_pixel_grid(parent, tile).crs_code == "EPSG:32610"


class TestTileBbox:
    def test_nw_corner_tile(self) -> None:
        # Worked example from m11-pixel-grid.md.
        parent = _parent_grid(scale=1.0, translate_x=10.0, translate_y=40.0, width=20, height=20)
        tile = TileCoordinate(col_px=0, row_px=0, width_px=10, height_px=10)
        assert tile_bbox(parent, tile) == (10.0, 30.0, 20.0, 40.0)

    def test_se_corner_tile(self) -> None:
        parent = _parent_grid(scale=1.0, translate_x=10.0, translate_y=40.0, width=20, height=20)
        tile = TileCoordinate(col_px=10, row_px=10, width_px=10, height_px=10)
        assert tile_bbox(parent, tile) == (20.0, 20.0, 30.0, 30.0)

    def test_subpixel_tile(self) -> None:
        # A split child: half-size at offset (5, 5).
        parent = _parent_grid(scale=1.0, translate_x=0.0, translate_y=10.0, width=10, height=10)
        tile = TileCoordinate(col_px=5, row_px=5, width_px=5, height_px=5)
        # x_min = 0 + 5*1 = 5; x_max = 5 + 5*1 = 10
        # y_max = 10 + 5*(-1) = 5; y_min = 5 + 5*(-1) = 0
        assert tile_bbox(parent, tile) == (5.0, 0.0, 10.0, 5.0)


# ---------------------------------------------------------------------------
# Snap and alignment
# ---------------------------------------------------------------------------


def test_tiles_are_full_size() -> None:
    grid = decompose_region(SMALL_SQUARE, scale_meters=30.0, tile_size_pixels=256)
    for tile in grid.tiles:
        assert tile.width_px == 256
        assert tile.height_px == 256


def test_tiles_offsets_are_tile_size_multiples() -> None:
    grid = decompose_region(SMALL_SQUARE, scale_meters=30.0, tile_size_pixels=256)
    for tile in grid.tiles:
        assert tile.col_px % 256 == 0
        assert tile.row_px % 256 == 0


def test_parent_translate_is_global_snap() -> None:
    """The parent grid's translate must be a tile-size multiple of pixel size,
    measured against a global (0, 0) origin in CRS units."""
    grid = decompose_region(SMALL_SQUARE, scale_meters=30.0, tile_size_pixels=256)
    pixel_size = grid.pixel_size
    tile_size_native = pixel_size * 256
    tx = grid.pixel_grid.affine_transform.translate_x
    ty = grid.pixel_grid.affine_transform.translate_y
    assert math.isclose(tx / tile_size_native, round(tx / tile_size_native), abs_tol=1e-9)
    assert math.isclose(ty / tile_size_native, round(ty / tile_size_native), abs_tol=1e-9)


def test_grid_alignment_unconditional_across_latitudes() -> None:
    """Two exports at very different centroid latitudes must produce identical
    pixel sizes and tile offsets — that's the whole point of M11. The parent
    grid translate differs (different bbox) but the affine scale is the same,
    and shared tiles snap to the same multiple of (pixel_size * tile_size)."""
    region_low_lat = {
        "type": "Polygon",
        "coordinates": [
            [
                [0.0, 0.0],
                [1.0, 0.0],
                [1.0, 1.0],
                [0.0, 1.0],
                [0.0, 0.0],
            ]
        ],
    }
    region_high_lat = {
        "type": "Polygon",
        "coordinates": [
            [
                [0.0, 60.0],
                [1.0, 60.0],
                [1.0, 61.0],
                [0.0, 61.0],
                [0.0, 60.0],
            ]
        ],
    }
    grid_low = decompose_region(region_low_lat, scale_meters=100.0, tile_size_pixels=256)
    grid_high = decompose_region(region_high_lat, scale_meters=100.0, tile_size_pixels=256)
    # Same CRS scale: derives from the equator constant for both.
    assert grid_low.pixel_size == grid_high.pixel_size
    # Both translates are multiples of tile_size_native from the global origin.
    tile_size_native = grid_low.pixel_size * 256
    for grid in (grid_low, grid_high):
        tx = grid.pixel_grid.affine_transform.translate_x
        assert math.isclose(tx / tile_size_native, round(tx / tile_size_native), abs_tol=1e-9)


def test_shifted_region_overlap_is_pixel_identical() -> None:
    """Overlapping tiles between two same-CRS, same-scale exports share an
    identical CRS bbox — global-origin snap means the pixel-to-CRS mapping is
    deterministic regardless of where the export bbox sits."""
    region_a = {
        "type": "Polygon",
        "coordinates": [[[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5], [0.0, 0.0]]],
    }
    region_b = {
        "type": "Polygon",
        "coordinates": [[[0.2, 0.0], [0.7, 0.0], [0.7, 0.5], [0.2, 0.5], [0.2, 0.0]]],
    }
    grid_a = decompose_region(region_a, scale_meters=100.0, tile_size_pixels=256)
    grid_b = decompose_region(region_b, scale_meters=100.0, tile_size_pixels=256)

    # Build absolute-bbox lookups — col_px/row_px are local to each parent
    # so we have to compose with translate to compare across exports.
    bboxes_a = {tile_bbox(grid_a.pixel_grid, t) for t in grid_a.tiles}
    bboxes_b = {tile_bbox(grid_b.pixel_grid, t) for t in grid_b.tiles}
    overlap = bboxes_a & bboxes_b
    assert len(overlap) > 0


def test_adjacent_tiles_share_pixel_boundary() -> None:
    """Compute tiles abutting in the grid have flush pixel offsets."""
    grid = decompose_region(CALIFORNIA_BBOX, scale_meters=1000.0, tile_size_pixels=256)
    by_rc = {(t.row, t.col): t for t in grid.tiles}
    for (row, col), tile in by_rc.items():
        right = by_rc.get((row, col + 1))
        if right:
            assert right.col_px == tile.col_px + tile.width_px
            assert right.row_px == tile.row_px
        below = by_rc.get((row + 1, col))
        if below:
            assert below.row_px == tile.row_px + tile.height_px
            assert below.col_px == tile.col_px


# ---------------------------------------------------------------------------
# M6 two-tier tiling: out_row / out_col assignment
# ---------------------------------------------------------------------------


def test_out_row_col_default_to_row_col_when_two_tier_disabled() -> None:
    grid = decompose_region(CALIFORNIA_BBOX, scale_meters=10000.0, tile_size_pixels=256)
    for t in grid.tiles:
        assert t.out_row == t.row
        assert t.out_col == t.col


def test_out_row_col_when_output_tile_equals_compute_tile() -> None:
    grid = decompose_region(
        CALIFORNIA_BBOX,
        scale_meters=10000.0,
        tile_size_pixels=256,
        output_tile_size_pixels=256,
    )
    for t in grid.tiles:
        assert t.out_row == t.row
        assert t.out_col == t.col


def test_out_row_col_groups_compute_tiles_into_output_tiles() -> None:
    grid = decompose_region(
        CALIFORNIA_BBOX,
        scale_meters=10000.0,
        tile_size_pixels=64,
        output_tile_size_pixels=256,  # 4×4 compute tiles per output tile
    )
    by_out: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for t in grid.tiles:
        by_out.setdefault((t.out_row, t.out_col), []).append((t.row, t.col))

    for members in by_out.values():
        assert len(members) <= 16
        rows = {r for r, _ in members}
        cols = {c for _, c in members}
        assert max(rows) - min(rows) <= 3
        assert max(cols) - min(cols) <= 3


def test_out_row_col_uses_floor_division_for_assignment() -> None:
    grid = decompose_region(
        SMALL_SQUARE,
        scale_meters=30.0,
        tile_size_pixels=64,
        output_tile_size_pixels=192,  # N = 3
    )
    n = 192 // 64
    by_out: dict[tuple[int, int], list[TileCoordinate]] = {}
    for t in grid.tiles:
        by_out.setdefault((t.out_row, t.out_col), []).append(t)
    for tiles in by_out.values():
        target_block_row = tiles[0].row // n
        target_block_col = tiles[0].col // n
        for t in tiles:
            assert t.row // n == target_block_row
            assert t.col // n == target_block_col


def test_invalid_output_tile_size_rejected() -> None:
    with pytest.raises(ValueError, match="must be a multiple"):
        decompose_region(
            SMALL_SQUARE,
            scale_meters=30.0,
            tile_size_pixels=64,
            output_tile_size_pixels=100,  # not a multiple of 64
        )


# ---------------------------------------------------------------------------
# _pixel_size_native: equator-constant for geographic, passthrough for projected
# ---------------------------------------------------------------------------


def test_pixel_size_native_geographic_uses_equator_constant() -> None:
    assert _pixel_size_native("EPSG:4326", 30.0) == pytest.approx(30.0 / _METERS_PER_DEGREE_EQUATOR)


def test_pixel_size_native_projected_passes_through() -> None:
    assert _pixel_size_native("EPSG:32610", 30.0) == 30.0
    assert _pixel_size_native("EPSG:3857", 100.0) == 100.0
