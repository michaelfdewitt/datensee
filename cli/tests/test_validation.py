"""Tests for the two-check output validation suite (integrity + pixels).

Covers both tiling modes:
- Non-two-tier (default): one COG per compute tile, named from compute indices.
- two-tier: one COG per output tile ``(out_row, out_col)``, sized
  ``OTS × OTS``, with compute tiles as windows inside it.

Tests that read real raster metadata/pixels require rasterio (the
``validation`` extra); they are skipped when it is not installed.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from datensee.config import (
    AffineTransform,
    GridDimensions,
    OutputConfig,
    PipelineConfig,
    PixelGrid,
    RunnerConfig,
    TileCoordinate,
    TileGrid,
)
from datensee.pixel.tiling import tile_pixel_grid
from datensee.pixel.validation import (
    TILES_FILE_SKIP_MESSAGE,
    CheckStatus,
    check_integrity,
    validate_output,
)
from datensee.pixel.validation.pixels import _build_hv_request, sample_evenly
from datensee.pixel.validation.units import (
    TILE_FILENAME_RE,
    OutputUnit,
    expected_output_units,
    is_m6,
    read_failure_keys,
    unit_filename,
)

_HAS_RASTERIO = importlib.util.find_spec("rasterio") is not None
requires_rasterio = pytest.mark.skipif(
    not _HAS_RASTERIO, reason="rasterio not installed (pip install datensee[validation])"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DUMMY_EXPRESSION = json.dumps({"result": "0", "values": {"0": {"constantValue": 1}}})

_TILE_SIZE = 64
# 0.1°/64 px so each compute tile is 0.1° on a side.
_PIXEL_SIZE = 0.1 / _TILE_SIZE


def _make_pixel_grid(rows: int, cols: int) -> PixelGrid:
    return PixelGrid(
        crs_code="EPSG:4326",
        affine_transform=AffineTransform(
            scale_x=_PIXEL_SIZE,
            shear_x=0.0,
            translate_x=0.0,
            shear_y=0.0,
            scale_y=-_PIXEL_SIZE,
            # NW corner: y_max for the largest row index.
            translate_y=rows * _TILE_SIZE * _PIXEL_SIZE,
        ),
        dimensions=GridDimensions(width=cols * _TILE_SIZE, height=rows * _TILE_SIZE),
    )


def _make_config(
    tiles: list[TileCoordinate],
    band_count: int = 1,
    data_type: str = "float32",
    output_path: str = "/tmp/test",
    output_tile_size_pixels: int | None = None,
) -> PipelineConfig:
    rows = max((t.row for t in tiles), default=0) + 1
    cols = max((t.col for t in tiles), default=0) + 1
    return PipelineConfig(
        ee_expression=_DUMMY_EXPRESSION,
        gee_project="test-project",
        tile_grid=TileGrid(
            pixel_grid=_make_pixel_grid(rows, cols),
            tile_size_pixels=_TILE_SIZE,
            tiles=tiles,
        ),
        output=OutputConfig(
            output_path=output_path,
            band_count=band_count,
            data_type=data_type,
            output_tile_size_pixels=output_tile_size_pixels,
        ),
        runner=RunnerConfig(mode="local"),
    )


def _make_tiles_file_config(output_path: str = "/tmp/test") -> PipelineConfig:
    return PipelineConfig(
        ee_expression=_DUMMY_EXPRESSION,
        gee_project="test-project",
        tile_grid=TileGrid(
            pixel_grid=_make_pixel_grid(1, 1),
            tile_size_pixels=_TILE_SIZE,
            tiles_file="/tmp/tiles.ndjson",
        ),
        output=OutputConfig(output_path=output_path),
        runner=RunnerConfig(mode="local"),
    )


def _make_tiles(rows: int, cols: int) -> list[TileCoordinate]:
    """Compute tiles for a non-two-tier grid (out indices == compute indices)."""
    return [
        TileCoordinate(
            col_px=c * _TILE_SIZE,
            row_px=r * _TILE_SIZE,
            width_px=_TILE_SIZE,
            height_px=_TILE_SIZE,
            row=r,
            col=c,
            out_row=r,
            out_col=c,
        )
        for r in range(rows)
        for c in range(cols)
    ]


def _make_m6_tiles(rows: int, cols: int, ots: int) -> list[TileCoordinate]:
    """Compute tiles stamped with two-tier output indices (out = px // OTS)."""
    return [
        TileCoordinate(
            col_px=c * _TILE_SIZE,
            row_px=r * _TILE_SIZE,
            width_px=_TILE_SIZE,
            height_px=_TILE_SIZE,
            row=r,
            col=c,
            out_row=(r * _TILE_SIZE) // ots,
            out_col=(c * _TILE_SIZE) // ots,
        )
        for r in range(rows)
        for c in range(cols)
    ]


def _write_fake_tiff(path: Path, size_bytes: int = 2048) -> None:
    path.write_bytes(b"II\x2a\x00" + b"\x00" * (size_bytes - 4))


def _write_journal(output_dir: Path, tiles: list[TileCoordinate]) -> None:
    lines = [json.dumps({**t.model_dump(), "error_kind": "AUTH_ERROR"}) for t in tiles]
    (output_dir / "_failures.json").write_text("\n".join(lines) + "\n")


def _write_geotiff(
    path: Path,
    data: np.ndarray,
    *,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
    crs: str = "EPSG:4326",
) -> None:
    """Write a real GeoTIFF; ``data`` is (bands, H, W) or (H, W)."""
    import rasterio
    from rasterio.transform import Affine

    bands = data if data.ndim == 3 else data[np.newaxis, ...]
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=bands.shape[1],
        width=bands.shape[2],
        count=bands.shape[0],
        dtype=str(bands.dtype),
        crs=crs,
        transform=Affine(_PIXEL_SIZE, 0.0, origin_x, 0.0, -_PIXEL_SIZE, origin_y),
    ) as ds:
        ds.write(bands)


def _unit_origin(config: PipelineConfig, unit: OutputUnit) -> tuple[float, float]:
    p = config.tile_grid.pixel_grid.affine_transform
    return (
        p.translate_x + unit.origin_col_px * p.scale_x,
        p.translate_y + unit.origin_row_px * p.scale_y,
    )


def _write_valid_unit(
    output_dir: Path, config: PipelineConfig, unit: OutputUnit, data: np.ndarray | None = None
) -> None:
    """Write an on-disk COG that fully satisfies the integrity check.

    Falls back to a magic-bytes-only fake when rasterio is unavailable
    (the check degrades symmetrically, so tests stay valid either way).
    """
    path = output_dir / unit.filename
    if not _HAS_RASTERIO:
        _write_fake_tiff(path)
        return
    if data is None:
        data = np.zeros(
            (config.output.band_count, unit.height_px, unit.width_px),
            dtype=config.output.data_type,
        )
    ox, oy = _unit_origin(config, unit)
    _write_geotiff(path, data, origin_x=ox, origin_y=oy, crs=config.tile_grid.crs)


def _units(config: PipelineConfig) -> list[OutputUnit]:
    units = expected_output_units(config)
    assert units is not None
    return units


# ---------------------------------------------------------------------------
# Output unit mapping
# ---------------------------------------------------------------------------


class TestOutputUnitMapping:
    def test_non_m6_one_unit_per_compute_tile(self) -> None:
        config = _make_config(_make_tiles(2, 3))
        assert not is_m6(config)
        units = _units(config)
        assert len(units) == 6
        u = next(u for u in units if u.key == (1, 2))
        assert u.filename == "tile_r0001_c0002.tif"
        assert (u.origin_col_px, u.origin_row_px) == (2 * _TILE_SIZE, _TILE_SIZE)
        assert (u.width_px, u.height_px) == (_TILE_SIZE, _TILE_SIZE)
        assert len(u.members) == 1

    def test_m6_groups_by_out_indices(self) -> None:
        ots = 2 * _TILE_SIZE
        config = _make_config(_make_m6_tiles(4, 4, ots), output_tile_size_pixels=ots)
        assert is_m6(config)
        units = _units(config)
        assert len(units) == 4  # 4x4 compute tiles → 2x2 output units
        u = next(u for u in units if u.key == (1, 1))
        assert u.filename == "tile_r0001_c0001.tif"
        assert (u.origin_col_px, u.origin_row_px) == (ots, ots)
        assert (u.width_px, u.height_px) == (ots, ots)
        assert len(u.members) == 4

    def test_ots_equal_to_tile_size_is_not_m6(self) -> None:
        config = _make_config(_make_tiles(1, 2), output_tile_size_pixels=_TILE_SIZE)
        assert not is_m6(config)
        assert len(_units(config)) == 2

    def test_split_child_unit_keeps_own_dimensions(self) -> None:
        child = TileCoordinate(
            col_px=32, row_px=0, width_px=32, height_px=16, row=0, col=0, lineage=[1]
        )
        units = _units(_make_config([child]))
        assert (units[0].width_px, units[0].height_px) == (32, 16)
        assert (units[0].origin_col_px, units[0].origin_row_px) == (32, 0)

    def test_tiles_file_returns_none(self) -> None:
        assert expected_output_units(_make_tiles_file_config()) is None

    def test_unit_filename_widens_and_roundtrips(self) -> None:
        assert unit_filename(3, 12) == "tile_r0003_c0012.tif"
        assert unit_filename(10000, 123456) == "tile_r10000_c123456.tif"
        m = TILE_FILENAME_RE.match("tile_r10000_c123456.tif")
        assert m is not None
        assert (int(m.group(1)), int(m.group(2))) == (10000, 123456)
        assert TILE_FILENAME_RE.match("tile_r001_c0002.tif") is None

    def test_read_failure_keys_skips_malformed_lines(self, tmp_path: Path) -> None:
        valid = json.dumps({"row": 1, "col": 2, "error_kind": "RATE_LIMITED"})
        (tmp_path / "_failures.json").write_text(valid + "\n{not valid json\n")
        assert read_failure_keys(tmp_path) == {(1, 2)}


# ---------------------------------------------------------------------------
# Integrity check
# ---------------------------------------------------------------------------


class TestIntegrity:
    @requires_rasterio
    def test_valid_output_passes(self, tmp_path: Path) -> None:
        config = _make_config(_make_tiles(1, 2), output_path=str(tmp_path))
        for u in _units(config):
            _write_valid_unit(tmp_path, config, u)
        result = check_integrity(tmp_path, config)
        assert result.status == CheckStatus.PASSED
        assert "All 2 expected output files" in result.message

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        config = _make_config(_make_tiles(2, 2), output_path=str(tmp_path))
        for u in _units(config)[:2]:
            _write_valid_unit(tmp_path, config, u)
        result = check_integrity(tmp_path, config)
        assert result.status == CheckStatus.FAILED
        assert any("file missing" in p["issues"] for p in result.details["problems"])

    def test_bad_magic_fails(self, tmp_path: Path) -> None:
        config = _make_config(_make_tiles(1, 1), output_path=str(tmp_path))
        (tmp_path / unit_filename(0, 0)).write_bytes(b"PK\x03\x04" + b"\x00" * 2000)
        result = check_integrity(tmp_path, config)
        assert result.status == CheckStatus.FAILED
        assert "bad magic" in str(result.details["problems"])

    def test_too_small_file_fails(self, tmp_path: Path) -> None:
        config = _make_config(_make_tiles(1, 1), output_path=str(tmp_path))
        (tmp_path / unit_filename(0, 0)).write_bytes(b"II\x2a\x00" + b"\x00" * 10)
        result = check_integrity(tmp_path, config)
        assert result.status == CheckStatus.FAILED
        assert "implausibly small" in str(result.details["problems"])

    @requires_rasterio
    def test_wrong_dimensions_fail(self, tmp_path: Path) -> None:
        config = _make_config(_make_tiles(1, 1), output_path=str(tmp_path))
        unit = _units(config)[0]
        ox, oy = _unit_origin(config, unit)
        _write_geotiff(
            tmp_path / unit.filename, np.zeros((32, 32), np.float32), origin_x=ox, origin_y=oy
        )
        result = check_integrity(tmp_path, config)
        assert result.status == CheckStatus.FAILED
        assert "dimensions 32x32, expected 64x64" in str(result.details["problems"])

    @requires_rasterio
    def test_wrong_origin_fails(self, tmp_path: Path) -> None:
        config = _make_config(_make_tiles(1, 1), output_path=str(tmp_path))
        unit = _units(config)[0]
        ox, oy = _unit_origin(config, unit)
        _write_geotiff(
            tmp_path / unit.filename,
            np.zeros((64, 64), np.float32),
            origin_x=ox + 1.0,
            origin_y=oy,
        )
        result = check_integrity(tmp_path, config)
        assert result.status == CheckStatus.FAILED
        assert "origin X" in str(result.details["problems"])

    @requires_rasterio
    def test_wrong_crs_fails(self, tmp_path: Path) -> None:
        config = _make_config(_make_tiles(1, 1), output_path=str(tmp_path))
        unit = _units(config)[0]
        ox, oy = _unit_origin(config, unit)
        _write_geotiff(
            tmp_path / unit.filename,
            np.zeros((64, 64), np.float32),
            origin_x=ox,
            origin_y=oy,
            crs="EPSG:32610",
        )
        result = check_integrity(tmp_path, config)
        assert result.status == CheckStatus.FAILED
        assert "CRS" in str(result.details["problems"])

    @requires_rasterio
    def test_wrong_band_count_and_dtype_fail(self, tmp_path: Path) -> None:
        config = _make_config(_make_tiles(1, 1), band_count=2, output_path=str(tmp_path))
        unit = _units(config)[0]
        _write_valid_unit(tmp_path, config, unit, data=np.zeros((1, 64, 64), np.float64))
        result = check_integrity(tmp_path, config)
        assert result.status == CheckStatus.FAILED
        assert "bands=1, expected 2" in str(result.details["problems"])
        assert "dtype=float64, expected float32" in str(result.details["problems"])

    def test_fully_journaled_unit_is_exempt(self, tmp_path: Path) -> None:
        tiles = _make_tiles(2, 2)
        config = _make_config(tiles, output_path=str(tmp_path))
        units = _units(config)
        for u in units[:3]:
            _write_valid_unit(tmp_path, config, u)
        _write_journal(tmp_path, [tiles[3]])
        result = check_integrity(tmp_path, config)
        assert result.status == CheckStatus.PASSED
        assert "1 known-failed" in result.message
        assert result.details["known_failed"] == [units[3].filename]

    def test_m6_partially_failed_unit_still_expected(self, tmp_path: Path) -> None:
        ots = 2 * _TILE_SIZE
        tiles = _make_m6_tiles(2, 2, ots)  # one output unit, 4 members
        config = _make_config(tiles, output_path=str(tmp_path), output_tile_size_pixels=ots)
        _write_journal(tmp_path, tiles[:2])  # 2 of 4 members failed
        result = check_integrity(tmp_path, config)
        assert result.status == CheckStatus.FAILED
        assert any("file missing" in p["issues"] for p in result.details["problems"])

    def test_m6_fully_failed_unit_is_exempt(self, tmp_path: Path) -> None:
        ots = 2 * _TILE_SIZE
        tiles = _make_m6_tiles(2, 4, ots)  # units (0,0) and (0,1)
        config = _make_config(tiles, output_path=str(tmp_path), output_tile_size_pixels=ots)
        _write_journal(tmp_path, [t for t in tiles if (t.out_row, t.out_col) == (0, 1)])
        _write_valid_unit(tmp_path, config, next(u for u in _units(config) if u.key == (0, 0)))
        result = check_integrity(tmp_path, config)
        assert result.status == CheckStatus.PASSED
        assert "known-failed" in result.message

    @requires_rasterio
    def test_m6_expects_output_tile_size(self, tmp_path: Path) -> None:
        """In two-tier mode the expected dimensions are OTS × OTS, not tile size."""
        ots = 2 * _TILE_SIZE
        config = _make_config(
            _make_m6_tiles(2, 2, ots), output_path=str(tmp_path), output_tile_size_pixels=ots
        )
        unit = _units(config)[0]
        ox, oy = _unit_origin(config, unit)
        _write_geotiff(
            tmp_path / unit.filename,
            np.zeros((_TILE_SIZE, _TILE_SIZE), np.float32),
            origin_x=ox,
            origin_y=oy,
        )
        result = check_integrity(tmp_path, config)
        assert result.status == CheckStatus.FAILED
        assert f"expected {ots}x{ots}" in str(result.details["problems"])

        _write_valid_unit(tmp_path, config, unit)  # overwrite with OTS-sized COG
        assert check_integrity(tmp_path, config).status == CheckStatus.PASSED

    @requires_rasterio
    def test_split_child_checked_against_own_dimensions(self, tmp_path: Path) -> None:
        child = TileCoordinate(
            col_px=32, row_px=0, width_px=32, height_px=16, row=0, col=0, lineage=[1]
        )
        config = _make_config([child], output_path=str(tmp_path))
        _write_valid_unit(tmp_path, config, _units(config)[0])
        assert check_integrity(tmp_path, config).status == CheckStatus.PASSED

    def test_unexpected_file_fails(self, tmp_path: Path) -> None:
        config = _make_config(_make_tiles(1, 1), output_path=str(tmp_path))
        _write_valid_unit(tmp_path, config, _units(config)[0])
        _write_fake_tiff(tmp_path / "tile_r0099_c0099.tif")
        result = check_integrity(tmp_path, config)
        assert result.status == CheckStatus.FAILED
        assert "match no expected unit" in result.message
        assert result.details["unexpected_files"] == ["tile_r0099_c0099.tif"]

    def test_degrades_without_rasterio(self, tmp_path: Path) -> None:
        """Without rasterio: existence/magic/size/accounting only, with a note."""
        config = _make_config(_make_tiles(1, 1), output_path=str(tmp_path))
        _write_fake_tiff(tmp_path / unit_filename(0, 0))  # no real metadata inside
        with patch("datensee.pixel.validation._has_rasterio", return_value=False):
            result = check_integrity(tmp_path, config)
        assert result.status == CheckStatus.PASSED
        assert "rasterio not installed" in result.message


# ---------------------------------------------------------------------------
# Pixels check
# ---------------------------------------------------------------------------

_FETCH = "datensee.pixel.validation.pixels._fetch_tile_as_numpy"
_TOKEN = "datensee.auth.get_access_token"


def _run_pixels(
    output_dir: Path, config: PipelineConfig, fetch: object, sample: int = 20
) -> tuple[object, object]:
    """validate_output(pixels=True) with fetch + auth mocked; returns both results."""
    kwargs = {"side_effect": fetch} if callable(fetch) else {"return_value": fetch}
    with patch(_FETCH, **kwargs), patch(_TOKEN, return_value="fake-token"):
        report = validate_output(str(output_dir), config, pixels=True, sample=sample)
    integrity, pixels = report.results
    assert integrity.check_id == "integrity"
    assert pixels.check_id == "pixels"
    return integrity, pixels


class TestPixels:
    def test_hv_request_matches_java_fortile(self) -> None:
        """Verbatim parent scale, translated origin, tile's own dimensions."""
        parent = PixelGrid(
            crs_code="EPSG:4326",
            affine_transform=AffineTransform(
                scale_x=0.001,
                shear_x=0.0,
                translate_x=5.0,
                shear_y=0.0,
                scale_y=-0.001,
                translate_y=8.0,
            ),
            dimensions=GridDimensions(width=256, height=256),
        )
        tile = TileCoordinate(col_px=128, row_px=64, width_px=32, height_px=16)
        body = _build_hv_request(_DUMMY_EXPRESSION, tile_pixel_grid(parent, tile))
        at = body["grid"]["affineTransform"]
        assert at["scaleX"] == 0.001
        assert at["scaleY"] == -0.001
        assert at["translateX"] == 5.0 + 128 * 0.001
        assert at["translateY"] == 8.0 + 64 * -0.001
        assert body["grid"]["dimensions"] == {"width": 32, "height": 16}
        assert body["grid"]["crsCode"] == "EPSG:4326"
        assert body["fileFormat"] == "NPY"

    def test_sample_evenly_first_last_deterministic(self) -> None:
        items = list(range(100))
        sampled = sample_evenly(items, 5)
        assert sampled[0] == 0
        assert sampled[-1] == 99
        assert len(sampled) == 5
        assert sampled == sample_evenly(items, 5)  # seed-free determinism
        assert sample_evenly(items, 200) == items  # fewer items than n → all

    @requires_rasterio
    def test_matching_pixels_pass(self, tmp_path: Path) -> None:
        config = _make_config(_make_tiles(1, 1), output_path=str(tmp_path))
        data = np.random.default_rng(42).uniform(0, 100, (64, 64)).astype(np.float32)
        _write_valid_unit(tmp_path, config, _units(config)[0], data=data[np.newaxis])
        integrity, pixels = _run_pixels(tmp_path, config, data)
        assert integrity.status == CheckStatus.PASSED
        assert pixels.status == CheckStatus.PASSED

    @requires_rasterio
    def test_mismatched_pixels_fail(self, tmp_path: Path) -> None:
        config = _make_config(_make_tiles(1, 1), output_path=str(tmp_path))
        _write_valid_unit(tmp_path, config, _units(config)[0])  # zeros on disk
        _, pixels = _run_pixels(tmp_path, config, np.ones((64, 64), np.float32))
        assert pixels.status == CheckStatus.FAILED
        assert "differ" in pixels.message
        assert pixels.details["mismatches"]

    @requires_rasterio
    def test_structured_response_compares_all_bands(self, tmp_path: Path) -> None:
        """EE's structured NPY — (H, W) with one field per band — is compared
        band-for-band; corrupting only band 2 must fail."""
        config = _make_config(_make_tiles(1, 1), band_count=2, output_path=str(tmp_path))
        band1 = np.arange(64 * 64, dtype=np.float32).reshape(64, 64)
        band2 = band1 * 2.0
        _write_valid_unit(tmp_path, config, _units(config)[0], data=np.stack([band1, band2]))

        structured = np.zeros((64, 64), dtype=[("elevation", "<f4"), ("slope", "<f4")])
        structured["elevation"] = band1
        structured["slope"] = band2
        _, pixels = _run_pixels(tmp_path, config, structured)
        assert pixels.status == CheckStatus.PASSED, pixels.details

        bad = structured.copy()
        bad["slope"] = band2 + 1.0
        _, pixels = _run_pixels(tmp_path, config, bad)
        assert pixels.status == CheckStatus.FAILED

    @requires_rasterio
    def test_m6_windowed_comparison(self, tmp_path: Path) -> None:
        """In two-tier mode a compute tile's on-disk data is a window of its unit COG."""
        ots = 2 * _TILE_SIZE
        tiles = _make_m6_tiles(2, 2, ots)  # one OTS×OTS unit, 4 members
        config = _make_config(tiles, output_path=str(tmp_path), output_tile_size_pixels=ots)
        data = np.zeros((ots, ots), np.float32)
        for t in tiles:  # distinct constant per quadrant
            data[t.row_px : t.row_px + _TILE_SIZE, t.col_px : t.col_px + _TILE_SIZE] = (
                10.0 * t.row + t.col
            )
        _write_valid_unit(tmp_path, config, _units(config)[0], data=data[np.newaxis])

        parent = config.tile_grid.pixel_grid.affine_transform

        def fetch_quadrant(client, project, token, expression, grid):  # noqa: ANN001, ANN202
            col_px = round((grid.affine_transform.translate_x - parent.translate_x) / _PIXEL_SIZE)
            row_px = round((parent.translate_y - grid.affine_transform.translate_y) / _PIXEL_SIZE)
            value = 10.0 * (row_px // _TILE_SIZE) + (col_px // _TILE_SIZE)
            return np.full((_TILE_SIZE, _TILE_SIZE), value, np.float32)

        _, pixels = _run_pixels(tmp_path, config, fetch_quadrant)
        assert pixels.status == CheckStatus.PASSED, pixels.details

        def fetch_wrong(client, project, token, expression, grid):  # noqa: ANN001, ANN202
            return fetch_quadrant(client, project, token, expression, grid) + 1.0

        _, pixels = _run_pixels(tmp_path, config, fetch_wrong)
        assert pixels.status == CheckStatus.FAILED

    @requires_rasterio
    def test_journaled_tiles_excluded_from_sample(self, tmp_path: Path) -> None:
        tiles = _make_tiles(1, 2)
        config = _make_config(tiles, output_path=str(tmp_path))
        _write_valid_unit(tmp_path, config, _units(config)[0])
        _write_journal(tmp_path, [tiles[1]])  # (0,1) failed → exempt everywhere

        fetched: list[object] = []

        def fetch(client, project, token, expression, grid):  # noqa: ANN001, ANN202
            fetched.append(grid)
            return np.zeros((64, 64), np.float32)

        integrity, pixels = _run_pixels(tmp_path, config, fetch)
        assert integrity.status == CheckStatus.PASSED
        assert pixels.status == CheckStatus.PASSED
        assert len(fetched) == 1  # only the live tile was re-fetched

    def test_no_comparable_tiles_skipped(self, tmp_path: Path) -> None:
        config = _make_config(_make_tiles(1, 1), output_path=str(tmp_path))
        # No files on disk: integrity fails, pixels has nothing to compare.
        _, pixels = _run_pixels(tmp_path, config, np.zeros((64, 64), np.float32))
        assert pixels.status == CheckStatus.SKIPPED
        assert "No tiles could be compared" in pixels.message


# ---------------------------------------------------------------------------
# validate_output API surface
# ---------------------------------------------------------------------------


class TestValidateOutput:
    def test_default_runs_integrity_only(self, tmp_path: Path) -> None:
        config = _make_config(_make_tiles(1, 1), output_path=str(tmp_path))
        _write_valid_unit(tmp_path, config, _units(config)[0])
        report = validate_output(str(tmp_path), config)
        assert [r.check_id for r in report.results] == ["integrity"]
        assert report.all_passed

    def test_tiles_file_config_skips_both_checks(self, tmp_path: Path) -> None:
        config = _make_tiles_file_config(output_path=str(tmp_path))
        with patch(_TOKEN, return_value="fake-token"):
            report = validate_output(tmp_path, config, pixels=True)
        assert [r.check_id for r in report.results] == ["integrity", "pixels"]
        assert all(r.status == CheckStatus.SKIPPED for r in report.results)
        assert all(r.message == TILES_FILE_SKIP_MESSAGE for r in report.results)
        assert report.all_passed  # skipped is not a failure

    def test_failed_check_flips_all_passed(self, tmp_path: Path) -> None:
        config = _make_config(_make_tiles(1, 1), output_path=str(tmp_path))
        report = validate_output(str(tmp_path), config)  # nothing on disk
        assert not report.all_passed

    def test_check_exception_becomes_error_result(self, tmp_path: Path) -> None:
        config = _make_config(_make_tiles(1, 1), output_path=str(tmp_path))
        with patch("datensee.pixel.validation.check_integrity", side_effect=RuntimeError("boom")):
            report = validate_output(str(tmp_path), config)
        assert report.results[0].status == CheckStatus.ERROR
        assert "boom" in report.results[0].message
        assert not report.all_passed

    def test_report_render_and_to_dict(self, tmp_path: Path) -> None:
        from rich.panel import Panel

        config = _make_config(_make_tiles(1, 1), output_path=str(tmp_path))
        _write_valid_unit(tmp_path, config, _units(config)[0])
        report = validate_output(str(tmp_path), config)
        assert isinstance(report.render(), Panel)
        d = report.to_dict()
        assert d["output_path"] == str(tmp_path)
        assert d["all_passed"] is True
        assert d["results"][0]["check_id"] == "integrity"
        assert d["results"][0]["status"] == "passed"


# ---------------------------------------------------------------------------
# gs:// output prefixes are staged locally, then validated with the same code
# ---------------------------------------------------------------------------


def test_validate_output_stages_gcs_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A gs:// output is mirrored into a temp dir; the report keeps the URI."""
    import tempfile

    from datensee.pixel import validation

    tiles = _make_tiles(1, 2)
    config = _make_config(tiles, output_path="gs://bucket/exports/run")
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    for unit in _units(config):
        _write_valid_unit(mirror, config, unit)

    seen: dict[str, str] = {}

    def fake_stage(
        gcs_prefix: str, *, credentials: object, max_bytes: int
    ) -> tempfile.TemporaryDirectory[str]:
        seen["prefix"], seen["max_bytes"] = gcs_prefix, max_bytes
        staging = tempfile.TemporaryDirectory()
        for path in mirror.iterdir():
            (Path(staging.name) / path.name).write_bytes(path.read_bytes())
        return staging

    monkeypatch.setattr(validation, "_stage_gcs_output", fake_stage)
    report = validation.validate_output("gs://bucket/exports/run", config)

    assert seen == {
        "prefix": "gs://bucket/exports/run",
        "max_bytes": validation.DEFAULT_MAX_STAGE_BYTES,
    }
    assert report.output_path == "gs://bucket/exports/run"
    assert report.all_passed, report.results[0].message


def test_validate_output_skips_gcs_staging_when_tiles_are_externalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both checks SKIP for tiles_file configs — so nothing must be downloaded."""
    from datensee.pixel import validation

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("staging must not run")

    monkeypatch.setattr(validation, "_stage_gcs_output", boom)
    report = validation.validate_output(
        "gs://bucket/big", _make_tiles_file_config("gs://bucket/big"), pixels=True
    )
    assert [r.status for r in report.results] == [CheckStatus.SKIPPED, CheckStatus.SKIPPED]


def test_validate_output_reports_staging_failure_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datensee.pixel import validation

    def forbidden(*args: object, **kwargs: object) -> None:
        raise PermissionError("403 storage.objects.list denied")

    monkeypatch.setattr(validation, "_stage_gcs_output", forbidden)
    report = validation.validate_output("gs://bucket/run", _make_config(_make_tiles(1, 1)))
    assert report.results[0].status == CheckStatus.ERROR
    assert "403" in report.results[0].message
    assert not report.all_passed
