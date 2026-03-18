"""Tests for region → tile grid decomposition."""

from datensee.tiling import decompose_region

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


def test_decompose_returns_at_least_one_tile() -> None:
    grid = decompose_region(SMALL_SQUARE, scale_meters=30.0)
    assert len(grid.tiles) >= 1


def test_decompose_tile_coordinates_are_within_bounds() -> None:
    grid = decompose_region(SMALL_SQUARE, scale_meters=30.0)
    for tile in grid.tiles:
        assert tile.x_min < tile.x_max
        assert tile.y_min < tile.y_max


def test_decompose_crs_is_preserved() -> None:
    grid = decompose_region(SMALL_SQUARE, scale_meters=30.0, crs="EPSG:4326")
    assert grid.crs == "EPSG:4326"


def test_decompose_scale_is_preserved() -> None:
    grid = decompose_region(SMALL_SQUARE, scale_meters=100.0)
    assert grid.scale_meters == 100.0


def test_large_scale_produces_fewer_tiles() -> None:
    grid_fine = decompose_region(CALIFORNIA_BBOX, scale_meters=30.0)
    grid_coarse = decompose_region(CALIFORNIA_BBOX, scale_meters=1000.0)
    assert len(grid_fine.tiles) > len(grid_coarse.tiles)


def test_tile_row_col_are_non_negative() -> None:
    grid = decompose_region(SMALL_SQUARE, scale_meters=30.0)
    for tile in grid.tiles:
        assert tile.row >= 0
        assert tile.col >= 0


# --- Projected CRS tests ---

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


def test_decompose_projected_crs_epsg32610() -> None:
    """UTM Zone 10N: tile coordinates should be in meters, not degrees."""
    grid = decompose_region(SF_BAY, scale_meters=10.0, crs="EPSG:32610", tile_size_pixels=256)
    assert grid.crs == "EPSG:32610"
    assert len(grid.tiles) >= 1

    # UTM Zone 10N coordinates for SF Bay Area are ~5e5 easting, ~4.2e6 northing
    for tile in grid.tiles:
        assert tile.x_min > 1000, "UTM easting should be in meters, not degrees"
        assert tile.y_min > 1000, "UTM northing should be in meters, not degrees"


def test_decompose_projected_crs_produces_different_tiles() -> None:
    """Same region in different CRS should produce different tile coordinates."""
    grid_geo = decompose_region(SF_BAY, scale_meters=30.0, crs="EPSG:4326")
    grid_utm = decompose_region(SF_BAY, scale_meters=30.0, crs="EPSG:32610")
    assert grid_geo.tiles[0].x_min != grid_utm.tiles[0].x_min


# --- Grid snapping tests ---


def test_tiles_are_full_size() -> None:
    """Every tile must be exactly tile_size_native wide — no edge clipping."""
    grid = decompose_region(SMALL_SQUARE, scale_meters=30.0, tile_size_pixels=256)
    from datensee.tiling import _pixel_size_native

    pixel = _pixel_size_native("EPSG:4326", 30.0)
    expected_size = pixel * 256

    for tile in grid.tiles:
        w = tile.x_max - tile.x_min
        h = tile.y_max - tile.y_min
        assert abs(w - expected_size) < 1e-10, f"Tile width {w} != {expected_size}"
        assert abs(h - expected_size) < 1e-10, f"Tile height {h} != {expected_size}"


def test_tiles_are_full_size_utm() -> None:
    """Same check in UTM: tiles must be exactly scale × tile_size meters."""
    grid = decompose_region(SF_BAY, scale_meters=10.0, crs="EPSG:32610", tile_size_pixels=256)
    expected_size = 10.0 * 256  # 2560 meters

    for tile in grid.tiles:
        w = tile.x_max - tile.x_min
        h = tile.y_max - tile.y_min
        assert abs(w - expected_size) < 1e-6, f"Tile width {w} != {expected_size}"
        assert abs(h - expected_size) < 1e-6, f"Tile height {h} != {expected_size}"


def test_grid_snapped_to_global_origin() -> None:
    """Tile boundaries must be multiples of tile_size_native from (0, 0)."""
    grid = decompose_region(SMALL_SQUARE, scale_meters=30.0, tile_size_pixels=256)
    from datensee.tiling import _pixel_size_native

    tile_size_native = _pixel_size_native("EPSG:4326", 30.0) * 256

    for tile in grid.tiles:
        # x_min / tile_size_native should be an integer
        col_idx = tile.x_min / tile_size_native
        row_idx = tile.y_min / tile_size_native
        assert abs(col_idx - round(col_idx)) < 1e-9, (
            f"x_min={tile.x_min} not snapped (col_idx={col_idx})"
        )
        assert abs(row_idx - round(row_idx)) < 1e-9, (
            f"y_min={tile.y_min} not snapped (row_idx={row_idx})"
        )


def test_grid_snapped_to_global_origin_utm() -> None:
    """UTM tile boundaries must be multiples of tile_size from 0."""
    grid = decompose_region(SF_BAY, scale_meters=10.0, crs="EPSG:32610", tile_size_pixels=256)
    tile_size = 10.0 * 256  # 2560 m

    for tile in grid.tiles:
        col_idx = tile.x_min / tile_size
        row_idx = tile.y_min / tile_size
        assert abs(col_idx - round(col_idx)) < 1e-9, (
            f"x_min={tile.x_min} not snapped (col_idx={col_idx})"
        )
        assert abs(row_idx - round(row_idx)) < 1e-9, (
            f"y_min={tile.y_min} not snapped (row_idx={row_idx})"
        )


def test_shifted_region_produces_aligned_grid() -> None:
    """Two overlapping regions must produce identical tile boundaries
    in the overlap area (because the grid is globally snapped)."""
    region_a = {
        "type": "Polygon",
        "coordinates": [[[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5], [0.0, 0.0]]],
    }
    region_b = {
        "type": "Polygon",
        "coordinates": [[[0.2, 0.2], [0.7, 0.2], [0.7, 0.7], [0.2, 0.7], [0.2, 0.2]]],
    }

    grid_a = decompose_region(region_a, scale_meters=100.0, tile_size_pixels=256)
    grid_b = decompose_region(region_b, scale_meters=100.0, tile_size_pixels=256)

    # Build lookup of tile bounds by (x_min, y_min)
    bounds_a = {(round(t.x_min, 10), round(t.y_min, 10)) for t in grid_a.tiles}
    bounds_b = {(round(t.x_min, 10), round(t.y_min, 10)) for t in grid_b.tiles}

    overlap = bounds_a & bounds_b
    assert len(overlap) > 0, "Expected overlapping tiles between shifted regions"

    # For overlapping tiles, verify exact coordinate match
    tiles_a_by_origin = {(round(t.x_min, 10), round(t.y_min, 10)): t for t in grid_a.tiles}
    tiles_b_by_origin = {(round(t.x_min, 10), round(t.y_min, 10)): t for t in grid_b.tiles}
    for origin in overlap:
        ta = tiles_a_by_origin[origin]
        tb = tiles_b_by_origin[origin]
        assert abs(ta.x_max - tb.x_max) < 1e-10
        assert abs(ta.y_max - tb.y_max) < 1e-10


def test_adjacent_tiles_share_boundaries() -> None:
    """Horizontally adjacent tiles must have tile_a.x_max == tile_b.x_min exactly."""
    grid = decompose_region(CALIFORNIA_BBOX, scale_meters=1000.0, tile_size_pixels=256)

    tiles_by_rc = {(t.row, t.col): t for t in grid.tiles}
    for (row, col), tile in tiles_by_rc.items():
        right = tiles_by_rc.get((row, col + 1))
        if right:
            assert abs(tile.x_max - right.x_min) < 1e-10, (
                f"Gap/overlap between ({row},{col}) and ({row},{col + 1}): "
                f"{tile.x_max} vs {right.x_min}"
            )
        above = tiles_by_rc.get((row + 1, col))
        if above:
            assert abs(tile.y_max - above.y_min) < 1e-10, (
                f"Gap/overlap between ({row},{col}) and ({row + 1},{col}): "
                f"{tile.y_max} vs {above.y_min}"
            )
