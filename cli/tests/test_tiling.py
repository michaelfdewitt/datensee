"""Tests for region → tile grid decomposition (PixelGrid shape)."""

from __future__ import annotations

import math

import pytest

from datensee.config import (
    AffineTransform,
    GridDimensions,
    PixelGrid,
    TileCoordinate,
)
from datensee.pixel.tiling import (
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
        # Worked example: 20x20 px region, pixel_size=1, tile_size=10.
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
    pixel sizes and tile offsets (the core invariant of canonical-grid design). The parent
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
    identical CRS bbox: global-origin snap means the pixel-to-CRS mapping is
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

    # Build absolute-bbox lookups: col_px/row_px are local to each parent
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
# two-tier tiling: out_row / out_col assignment
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


# Region chosen so the compute-tile snap alone would put the parent origin
# on an *odd* multiple of the tile size (i.e. NOT on an output-tile
# boundary). Pre-fix, this produced groups whose local offsets disagreed
# with the Java assembler's `(col_px // out) * out` origin snap.
OFFSET_SQUARE = {
    "type": "Polygon",
    "coordinates": [
        [
            [0.52, 0.32],
            [0.98, 0.32],
            [0.98, 0.78],
            [0.52, 0.78],
            [0.52, 0.32],
        ]
    ],
}


def test_m6_parent_origin_snaps_to_output_tile_boundary() -> None:
    """The assembler recovers each output tile's origin from local pixel
    offsets, which is only sound when the parent origin sits on an
    output-tile boundary. Pin that invariant for an awkward region."""
    out_px = 200
    grid = decompose_region(
        OFFSET_SQUARE,
        scale_meters=111.32,
        crs="EPSG:4326",
        tile_size_pixels=100,
        output_tile_size_pixels=out_px,
    )
    p = grid.pixel_grid.affine_transform
    origin_col_px = p.translate_x / p.scale_x
    origin_row_px = -p.translate_y / p.scale_x
    assert origin_col_px == pytest.approx(round(origin_col_px))
    assert round(origin_col_px) % out_px == 0
    assert round(origin_row_px) % out_px == 0


def test_m6_out_indices_match_local_offset_arithmetic() -> None:
    """out_row/out_col must equal (row_px // out, col_px // out), the exact
    arithmetic the Java assembler uses to place blocks and derive the
    output tile's affine. Every tile must fall inside its output rect."""
    out_px = 200
    tile_px = 100
    grid = decompose_region(
        OFFSET_SQUARE,
        scale_meters=111.32,
        crs="EPSG:4326",
        tile_size_pixels=tile_px,
        output_tile_size_pixels=out_px,
    )
    assert len(grid.tiles) > 4  # multi-group case, not degenerate
    for t in grid.tiles:
        assert t.out_col == t.col_px // out_px
        assert t.out_row == t.row_px // out_px
        # Block index within the output tile is in range by construction.
        bx = (t.col_px - t.out_col * out_px) // tile_px
        by = (t.row_px - t.out_row * out_px) // tile_px
        assert 0 <= bx < out_px // tile_px
        assert 0 <= by < out_px // tile_px


# ---------------------------------------------------------------------------
# _pixel_size_native: equator-constant for geographic, passthrough for projected
# ---------------------------------------------------------------------------


def test_pixel_size_native_geographic_uses_equator_constant() -> None:
    assert _pixel_size_native("EPSG:4326", 30.0) == pytest.approx(30.0 / _METERS_PER_DEGREE_EQUATOR)


def test_pixel_size_native_projected_passes_through() -> None:
    assert _pixel_size_native("EPSG:32610", 30.0) == 30.0
    assert _pixel_size_native("EPSG:3857", 100.0) == 100.0


# ---------------------------------------------------------------------------
# Exact-grid decomposition (decompose_pixel_grid / tiles_for_grid)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Exact-grid decomposition (decompose_pixel_grid / tiles_for_grid)
#
# Exact grid alignment ensures consistent cross-export pixel comparisons, so
# this is tested across: verbatim preservation, per-tile transforms,
# divisibility rules, two-tier partial edges, region filtering, and coordinate
# round-tripping.
# ---------------------------------------------------------------------------

from datensee.pixel.tiling import (  # noqa: E402
    decompose_pixel_grid,
    tiles_for_grid,
)


def _utm_grid(
    width: int, height: int, *, e0: float = 500000.0, n1: float = 4200000.0, scale: float = 10.0
) -> PixelGrid:
    return PixelGrid(
        crs_code="EPSG:32610",
        affine_transform=AffineTransform(
            scale_x=scale, translate_x=e0, scale_y=-scale, translate_y=n1
        ),
        dimensions=GridDimensions(width=width, height=height),
    )


class TestExactGridPreservation:
    def test_parent_grid_is_returned_verbatim(self) -> None:
        grid = _utm_grid(1024, 1024)
        tg = decompose_pixel_grid(grid, tile_size_pixels=512)
        assert tg.pixel_grid == grid  # byte-identical: no snapping, no re-derivation
        assert tg.tile_size_pixels == 512

    def test_tile_count_and_indices(self) -> None:
        tg = decompose_pixel_grid(_utm_grid(1536, 1024), tile_size_pixels=512)
        assert len(tg.tiles) == 3 * 2
        assert {(t.row, t.col) for t in tg.tiles} == {(r, c) for r in range(2) for c in range(3)}

    def test_tiles_are_full_size_and_offsets_are_multiples(self) -> None:
        tg = decompose_pixel_grid(_utm_grid(1024, 1536), tile_size_pixels=512)
        for t in tg.tiles:
            assert (t.width_px, t.height_px) == (512, 512)
            assert t.col_px % 512 == 0 and t.row_px % 512 == 0
            assert t.col_px == t.col * 512 and t.row_px == t.row * 512

    def test_non_square_tile_grid(self) -> None:
        # 3 wide x 1 tall
        tg = decompose_pixel_grid(_utm_grid(1536, 512), tile_size_pixels=512)
        assert len(tg.tiles) == 3
        assert max(t.col for t in tg.tiles) == 2
        assert max(t.row for t in tg.tiles) == 0

    def test_single_tile_grid(self) -> None:
        tg = decompose_pixel_grid(_utm_grid(512, 512), tile_size_pixels=512)
        assert len(tg.tiles) == 1
        assert (tg.tiles[0].row, tg.tiles[0].col) == (0, 0)

    def test_geographic_crs_grid_preserved(self) -> None:
        grid = PixelGrid(
            crs_code="EPSG:4326",
            affine_transform=AffineTransform(
                scale_x=0.001, translate_x=-122.5, scale_y=-0.001, translate_y=38.0
            ),
            dimensions=GridDimensions(width=1024, height=512),
        )
        tg = decompose_pixel_grid(grid, tile_size_pixels=512)
        assert tg.pixel_grid == grid
        assert len(tg.tiles) == 2


class TestExactGridPerTileTransform:
    """Each tile's per-fetch grid is the parent transform shifted to its NW
    corner: this is sent verbatim to computePixels."""

    def test_every_tile_transform_is_exact(self) -> None:
        grid = _utm_grid(1024, 1024, e0=512340.0, n1=4183400.0, scale=10.0)
        tg = decompose_pixel_grid(grid, tile_size_pixels=512)
        for t in tg.tiles:
            pg = tile_pixel_grid(tg.pixel_grid, t)
            a = pg.affine_transform
            assert a.scale_x == 10.0 and a.scale_y == -10.0
            # NW corner = parent origin + integer pixel offset * scale, exact.
            assert a.translate_x == 512340.0 + t.col_px * 10.0
            assert a.translate_y == 4183400.0 - t.row_px * 10.0
            assert (pg.dimensions.width, pg.dimensions.height) == (512, 512)

    def test_tiles_tile_the_grid_without_gaps_or_overlap(self) -> None:
        grid = _utm_grid(1024, 1536)
        tg = decompose_pixel_grid(grid, tile_size_pixels=512)
        covered = {
            (t.col_px + dx, t.row_px + dy) for t in tg.tiles for dx in (0, 511) for dy in (0, 511)
        }
        # 6 tiles × 4 corner-ish samples, all distinct → no overlap.
        assert len(covered) == len(tg.tiles) * 4
        # And the union spans exactly [0,1024) × [0,1536).
        assert max(t.col_px for t in tg.tiles) + 512 == 1024
        assert max(t.row_px for t in tg.tiles) + 512 == 1536


class TestExactGridDivisibility:
    def test_width_not_multiple_of_tile_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="whole multiple of tile_size"):
            decompose_pixel_grid(_utm_grid(1000, 1024), tile_size_pixels=512)

    def test_height_not_multiple_of_tile_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="whole multiple of tile_size"):
            decompose_pixel_grid(_utm_grid(1024, 700), tile_size_pixels=512)

    def test_output_tile_size_not_multiple_of_tile_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="multiple of tile_size"):
            decompose_pixel_grid(
                _utm_grid(1024, 1024), tile_size_pixels=512, output_tile_size_pixels=600
            )

    def test_odd_tile_size_that_divides_is_accepted(self) -> None:
        # 900 = 3*300; a non-power-of-two tile size must still work.
        tg = decompose_pixel_grid(_utm_grid(900, 300), tile_size_pixels=300)
        assert len(tg.tiles) == 3


class TestExactGridTwoTier:
    def test_out_indices_are_floor_of_local_offset(self) -> None:
        # tile 512, output 1024 → 2 compute tiles per output tile per axis.
        tg = decompose_pixel_grid(
            _utm_grid(2048, 1024), tile_size_pixels=512, output_tile_size_pixels=1024
        )
        for t in tg.tiles:
            assert t.out_col == t.col_px // 1024
            assert t.out_row == t.row_px // 1024
        assert {(t.out_row, t.out_col) for t in tg.tiles} == {(0, 0), (0, 1)}

    def test_partial_final_output_tile_allowed(self) -> None:
        # 1536 = 3*512 wide, output 1024 → out cols {0 (2 tiles), 1 (1 tile)}.
        tg = decompose_pixel_grid(
            _utm_grid(1536, 512), tile_size_pixels=512, output_tile_size_pixels=1024
        )
        by_out = {}
        for t in tg.tiles:
            by_out.setdefault(t.out_col, []).append(t)
        assert sorted(by_out) == [0, 1]
        assert len(by_out[0]) == 2 and len(by_out[1]) == 1

    def test_non_two_tier_out_indices_mirror_compute_indices(self) -> None:
        tg = decompose_pixel_grid(_utm_grid(1024, 1024), tile_size_pixels=512)
        for t in tg.tiles:
            assert (t.out_row, t.out_col) == (t.row, t.col)


class TestExactGridRegionFilter:
    def _wgs_box_over_utm(self, e_lo, e_hi, n_lo, n_hi) -> dict:
        from pyproj import Transformer

        to_wgs = Transformer.from_crs("EPSG:32610", "EPSG:4326", always_xy=True)
        corners = [to_wgs.transform(e, n) for e in (e_lo, e_hi) for n in (n_lo, n_hi)]
        lons = [c[0] for c in corners]
        lats = [c[1] for c in corners]
        return {
            "type": "Polygon",
            "coordinates": [
                [
                    [min(lons), min(lats)],
                    [max(lons), min(lats)],
                    [max(lons), max(lats)],
                    [min(lons), max(lats)],
                    [min(lons), min(lats)],
                ]
            ],
        }

    def test_no_region_tiles_whole_grid(self) -> None:
        tg = decompose_pixel_grid(_utm_grid(1024, 1024), tile_size_pixels=512)
        assert len(tg.tiles) == 4

    def test_region_covering_whole_grid_keeps_all_tiles(self) -> None:
        grid = _utm_grid(1024, 1024)
        region = self._wgs_box_over_utm(499000, 511000, 4188000, 4201000)
        tg = decompose_pixel_grid(grid, tile_size_pixels=512, geojson_geometry=region)
        assert len(tg.tiles) == 4

    def test_region_over_one_corner_trims_tiles(self) -> None:
        grid = _utm_grid(1024, 1024)
        # NW compute tile only: 500000..505120 E, 4194880..4200000 N.
        region = self._wgs_box_over_utm(500100, 505000, 4195000, 4199900)
        tg = decompose_pixel_grid(grid, tile_size_pixels=512, geojson_geometry=region)
        assert 0 < len(tg.tiles) < 4

    def test_non_intersecting_region_raises(self) -> None:
        far = {
            "type": "Polygon",
            "coordinates": [[[10.0, 10.0], [10.1, 10.0], [10.1, 10.1], [10.0, 10.1], [10.0, 10.0]]],
        }
        with pytest.raises(ValueError, match="does not intersect"):
            decompose_pixel_grid(_utm_grid(1024, 1024), tile_size_pixels=512, geojson_geometry=far)


class TestExactVsScaleGridIndependence:
    """The exact grid bypasses scale derivation, snapping, and the equator
    constant: why a scale export and an asset can differ by a pixel."""

    def test_exact_grid_origin_is_not_snapped_to_global(self) -> None:
        # A deliberately un-aligned origin (not a multiple of scale*tilesize)
        # is preserved exactly; decompose_region would have snapped it.
        grid = _utm_grid(1024, 1024, e0=512345.0, n1=4183397.0)
        tg = decompose_pixel_grid(grid, tile_size_pixels=512)
        assert tg.pixel_grid.affine_transform.translate_x == 512345.0
        assert tg.pixel_grid.affine_transform.translate_y == 4183397.0

    def test_two_exports_on_same_grid_are_pixel_identical(self) -> None:
        grid = _utm_grid(1024, 1024)
        a = decompose_pixel_grid(grid, tile_size_pixels=512)
        b = decompose_pixel_grid(grid, tile_size_pixels=512)
        assert a.pixel_grid == b.pixel_grid
        assert [t.model_dump() for t in a.tiles] == [t.model_dump() for t in b.tiles]

    def test_tiles_for_grid_and_decompose_pixel_grid_agree(self) -> None:
        grid = _utm_grid(1024, 512)
        via_public = decompose_pixel_grid(grid, tile_size_pixels=512)
        via_direct = tiles_for_grid(grid, tile_size_pixels=512)
        assert via_public.pixel_grid == via_direct.pixel_grid
        assert len(via_public.tiles) == len(via_direct.tiles) == 2
