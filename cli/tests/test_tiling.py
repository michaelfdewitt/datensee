"""Tests for region → tile grid decomposition."""

import pytest

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
