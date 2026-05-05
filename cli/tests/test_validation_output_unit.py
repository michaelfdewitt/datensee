"""Unit tests for output validation checks — synthetic GeoTIFFs, no network."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import numpy as np

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
from datensee.validation.assembly import (
    check_e08_failure_accounting,
    check_e10_size_plausibility,
)
from datensee.validation.catalog import CheckID, CostTier, get_check, list_checks, zero_cost_checks
from datensee.validation.report import CheckResult, CheckStatus, ValidationReport
from datensee.validation.sampling import SamplingStrategy, sample_tiles
from datensee.validation.tiff import validate_tiff_magic
from datensee.validation.tile_integrity import check_e01_tile_file_integrity, tile_filename

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DUMMY_EXPRESSION = json.dumps({"result": "0", "values": {"0": {"constantValue": 1}}})


_TILE_SIZE = 64
# Pixel size is 0.1°/64 px so each tile is 0.1° on a side — keeps the
# legacy values intact while moving to integer-pixel offsets.
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
) -> PipelineConfig:
    """Build a minimal PipelineConfig for testing.

    Parent grid dimensions are inferred from the supplied tiles.
    """
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
        ),
        runner=RunnerConfig(mode="local"),
    )


def _make_tiles(rows: int, cols: int) -> list[TileCoordinate]:
    """Generate a grid of tile coordinates.

    With NW-corner origin, ``row=0`` is the northernmost tile (smallest
    ``row_px``) and tiles are laid out row-by-row going south.
    """
    tiles = []
    for r in range(rows):
        for c in range(cols):
            tiles.append(
                TileCoordinate(
                    col_px=c * _TILE_SIZE,
                    row_px=r * _TILE_SIZE,
                    width_px=_TILE_SIZE,
                    height_px=_TILE_SIZE,
                    row=r,
                    col=c,
                )
            )
    return tiles


def _write_fake_tiff(path: Path, size_bytes: int = 2048) -> None:
    """Write a file with TIFF magic bytes (little-endian) and padding."""
    data = b"II" + b"\x2a\x00" + b"\x00" * (size_bytes - 4)
    path.write_bytes(data)


def _transform_for(
    config: PipelineConfig, tile: TileCoordinate
) -> tuple[float, float, float, float, float, float]:
    """Build a (scaleX, shearX, txX, shearY, scaleY, txY) transform that
    matches the tile's expected NW-corner CRS coordinates."""
    from datensee.tiling import tile_bbox

    x_min, _, _, y_max = tile_bbox(config.tile_grid.pixel_grid, tile)
    return (_PIXEL_SIZE, 0.0, x_min, 0.0, -_PIXEL_SIZE, y_max)


# ---------------------------------------------------------------------------
# Catalog tests
# ---------------------------------------------------------------------------


class TestCatalog:
    def test_list_evals_returns_all_eight(self) -> None:
        # E05 / E06 (VRT-related) were removed when the pipeline stopped
        # producing a VRT manifest.
        assert len(list_checks()) == 8

    def test_zero_cost_excludes_e07(self) -> None:
        zero = zero_cost_checks()
        assert CheckID.E07 not in zero
        assert CheckID.E01 in zero

    def test_get_eval_returns_definition(self) -> None:
        defn = get_check(CheckID.E01)
        assert defn.name == "Tile File Integrity"
        assert defn.cost_tier == CostTier.ZERO_COST

    def test_e07_is_api_cost(self) -> None:
        assert get_check(CheckID.E07).cost_tier == CostTier.API_COST


# ---------------------------------------------------------------------------
# Sampling tests
# ---------------------------------------------------------------------------


class TestSampling:
    def test_all_strategy_returns_everything(self) -> None:
        tiles = _make_tiles(3, 3)
        result = sample_tiles(tiles, strategy=SamplingStrategy.ALL)
        assert len(result) == 9

    def test_random_respects_n(self) -> None:
        tiles = _make_tiles(10, 10)
        result = sample_tiles(tiles, strategy=SamplingStrategy.RANDOM, n=5)
        assert len(result) == 5

    def test_random_deterministic_with_seed(self) -> None:
        tiles = _make_tiles(10, 10)
        a = sample_tiles(tiles, strategy=SamplingStrategy.RANDOM, n=5, seed=42)
        b = sample_tiles(tiles, strategy=SamplingStrategy.RANDOM, n=5, seed=42)
        assert [(t.row, t.col) for t in a] == [(t.row, t.col) for t in b]

    def test_stratified_includes_corners(self) -> None:
        tiles = _make_tiles(10, 10)
        result = sample_tiles(tiles, strategy=SamplingStrategy.STRATIFIED, n=20)
        coords = {(t.row, t.col) for t in result}
        # All 4 corners should be present
        assert (0, 0) in coords
        assert (0, 9) in coords
        assert (9, 0) in coords
        assert (9, 9) in coords

    def test_stratified_respects_n(self) -> None:
        tiles = _make_tiles(10, 10)
        result = sample_tiles(tiles, strategy=SamplingStrategy.STRATIFIED, n=10)
        assert len(result) == 10

    def test_small_grid_returns_all(self) -> None:
        tiles = _make_tiles(2, 2)
        result = sample_tiles(tiles, strategy=SamplingStrategy.STRATIFIED, n=20)
        assert len(result) == 4

    def test_empty_tiles(self) -> None:
        assert sample_tiles([], strategy=SamplingStrategy.STRATIFIED) == []


# ---------------------------------------------------------------------------
# TIFF magic validation
# ---------------------------------------------------------------------------


class TestTiffMagic:
    def test_valid_little_endian(self, tmp_path: Path) -> None:
        f = tmp_path / "test.tif"
        f.write_bytes(b"II\x2a\x00" + b"\x00" * 100)
        assert validate_tiff_magic(f) is True

    def test_valid_big_endian(self, tmp_path: Path) -> None:
        f = tmp_path / "test.tif"
        f.write_bytes(b"MM\x00\x2a" + b"\x00" * 100)
        assert validate_tiff_magic(f) is True

    def test_invalid_magic(self, tmp_path: Path) -> None:
        f = tmp_path / "test.tif"
        f.write_bytes(b"PK\x03\x04" + b"\x00" * 100)  # ZIP magic
        assert validate_tiff_magic(f) is False

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        assert validate_tiff_magic(tmp_path / "nope.tif") is False


# ---------------------------------------------------------------------------
# E01: Tile File Integrity
# ---------------------------------------------------------------------------


class TestE01TileFileIntegrity:
    def test_all_tiles_present_and_valid(self, tmp_path: Path) -> None:
        tiles = _make_tiles(2, 2)
        for t in tiles:
            _write_fake_tiff(tmp_path / tile_filename(t))

        result = check_e01_tile_file_integrity(tmp_path, tiles)
        assert result.status == CheckStatus.PASSED
        assert result.check_id == CheckID.E01

    def test_missing_tiles(self, tmp_path: Path) -> None:
        tiles = _make_tiles(2, 2)
        # Only write 2 of 4 tiles
        for t in tiles[:2]:
            _write_fake_tiff(tmp_path / tile_filename(t))

        result = check_e01_tile_file_integrity(tmp_path, tiles)
        assert result.status == CheckStatus.FAILED
        assert "2 missing" in result.message

    def test_bad_magic_bytes(self, tmp_path: Path) -> None:
        tiles = _make_tiles(1, 2)
        _write_fake_tiff(tmp_path / tile_filename(tiles[0]))
        # Write a non-TIFF file
        (tmp_path / tile_filename(tiles[1])).write_bytes(b"PK" + b"\x00" * 2000)

        result = check_e01_tile_file_integrity(tmp_path, tiles)
        assert result.status == CheckStatus.FAILED
        assert "1 invalid TIFF" in result.message

    def test_too_small_file(self, tmp_path: Path) -> None:
        tiles = _make_tiles(1, 1)
        (tmp_path / tile_filename(tiles[0])).write_bytes(b"II\x2a\x00" + b"\x00" * 10)

        result = check_e01_tile_file_integrity(tmp_path, tiles)
        assert result.status == CheckStatus.FAILED
        assert "too small" in result.message

    def test_empty_tile_list(self, tmp_path: Path) -> None:
        result = check_e01_tile_file_integrity(tmp_path, [])
        assert result.status == CheckStatus.PASSED


# ---------------------------------------------------------------------------
# E08: Failure Accounting
# ---------------------------------------------------------------------------


class TestE08FailureAccounting:
    def test_all_tiles_on_disk(self, tmp_path: Path) -> None:
        tiles = _make_tiles(2, 3)
        config = _make_config(tiles, output_path=str(tmp_path))
        for t in tiles:
            _write_fake_tiff(tmp_path / tile_filename(t))

        result = check_e08_failure_accounting(tmp_path, config)
        assert result.status == CheckStatus.PASSED
        assert "6 tiles accounted for" in result.message

    def test_some_failures_accounted(self, tmp_path: Path) -> None:
        tiles = _make_tiles(2, 2)
        config = _make_config(tiles, output_path=str(tmp_path))

        # Write 3 of 4 tiles to disk
        for t in tiles[:3]:
            _write_fake_tiff(tmp_path / tile_filename(t))

        # Record the 4th as a failure (NDJSON, one record per line — same shape
        # the Java pipeline writes via FailedTileWriter).
        missing = tiles[3]
        record = {"row": missing.row, "col": missing.col, "error": "timeout"}
        (tmp_path / "_failures.json").write_text(json.dumps(record) + "\n")

        result = check_e08_failure_accounting(tmp_path, config)
        assert result.status == CheckStatus.PASSED

    def test_unaccounted_tiles(self, tmp_path: Path) -> None:
        tiles = _make_tiles(2, 2)
        config = _make_config(tiles, output_path=str(tmp_path))

        # Only write 2 of 4 tiles, no _failures.json
        for t in tiles[:2]:
            _write_fake_tiff(tmp_path / tile_filename(t))

        result = check_e08_failure_accounting(tmp_path, config)
        assert result.status == CheckStatus.FAILED
        assert "missing" in result.message

    def test_unexpected_tiles(self, tmp_path: Path) -> None:
        tiles = _make_tiles(1, 1)
        config = _make_config(tiles, output_path=str(tmp_path))

        # Write the expected tile plus an extra one
        _write_fake_tiff(tmp_path / tile_filename(tiles[0]))
        _write_fake_tiff(tmp_path / "tile_r0099_c0099.tif")

        result = check_e08_failure_accounting(tmp_path, config)
        assert result.status == CheckStatus.FAILED
        assert "not in config" in result.message

    def test_no_failures_json(self, tmp_path: Path) -> None:
        """Missing _failures.json is fine — means zero failures."""
        tiles = _make_tiles(1, 1)
        config = _make_config(tiles, output_path=str(tmp_path))
        _write_fake_tiff(tmp_path / tile_filename(tiles[0]))

        result = check_e08_failure_accounting(tmp_path, config)
        assert result.status == CheckStatus.PASSED

    def test_multi_record_ndjson_failures_journal(self, tmp_path: Path) -> None:
        """E08 must read the NDJSON _failures.json the Java pipeline writes —
        one JSON object per line — so multi-failure runs are accounted for."""
        tiles = _make_tiles(2, 2)
        config = _make_config(tiles, output_path=str(tmp_path))

        # Write 2 of 4 tiles to disk; the other 2 are dead-lettered.
        for t in tiles[:2]:
            _write_fake_tiff(tmp_path / tile_filename(t))

        ndjson = (
            "\n".join(
                json.dumps({"row": t.row, "col": t.col, "error_kind": "RATE_LIMITED"})
                for t in tiles[2:]
            )
            + "\n"
        )
        (tmp_path / "_failures.json").write_text(ndjson)

        result = check_e08_failure_accounting(tmp_path, config)
        assert result.status == CheckStatus.PASSED
        assert "4 tiles accounted for" in result.message

    def test_malformed_line_does_not_poison_journal(self, tmp_path: Path) -> None:
        """A single corrupt line (e.g. partial flush at end of job) must
        not hide the valid records that precede it — the whole point of
        NDJSON is per-record robustness."""
        tiles = _make_tiles(2, 2)
        config = _make_config(tiles, output_path=str(tmp_path))

        # 3 tiles on disk, 1 valid failure, 1 corrupt trailing line.
        for t in tiles[:3]:
            _write_fake_tiff(tmp_path / tile_filename(t))
        valid = json.dumps({"row": tiles[3].row, "col": tiles[3].col, "error_kind": "RATE_LIMITED"})
        (tmp_path / "_failures.json").write_text(valid + "\n{not valid json\n")

        result = check_e08_failure_accounting(tmp_path, config)
        assert result.status == CheckStatus.PASSED
        assert "4 tiles accounted for" in result.message


# ---------------------------------------------------------------------------
# E10: Output Size Plausibility
# ---------------------------------------------------------------------------


class TestE10SizePlausibility:
    def test_size_within_bounds(self, tmp_path: Path) -> None:
        tiles = _make_tiles(2, 2)
        config = _make_config(tiles, output_path=str(tmp_path))

        # raw_output_bytes: 4 tiles * 64*64 * 4 bytes = 65,536 bytes
        # Write tiles at ~8 KB each → 32 KB total → ratio ≈ 0.5 (within bounds)
        for t in tiles:
            _write_fake_tiff(tmp_path / tile_filename(t), size_bytes=8192)

        result = check_e10_size_plausibility(tmp_path, config)
        assert result.status == CheckStatus.PASSED

    def test_size_too_small(self, tmp_path: Path) -> None:
        tiles = _make_tiles(2, 2)
        config = _make_config(tiles, output_path=str(tmp_path))

        # Write tiny tiles (100 bytes each) → way below 0.2x
        for t in tiles:
            _write_fake_tiff(tmp_path / tile_filename(t), size_bytes=100)

        result = check_e10_size_plausibility(tmp_path, config)
        assert result.status == CheckStatus.FAILED
        assert "smaller" in result.message

    def test_size_too_large(self, tmp_path: Path) -> None:
        tiles = _make_tiles(2, 2)
        config = _make_config(tiles, output_path=str(tmp_path))

        # Predicted ~32 KB. Write 500 KB each → 2 MB → >5x
        for t in tiles:
            _write_fake_tiff(tmp_path / tile_filename(t), size_bytes=500_000)

        result = check_e10_size_plausibility(tmp_path, config)
        assert result.status == CheckStatus.FAILED
        assert "larger" in result.message


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


class TestValidationReport:
    def test_all_passed(self) -> None:
        tiles = _make_tiles(1, 1)
        config = _make_config(tiles)
        report = ValidationReport(
            results=[
                CheckResult(check_id=CheckID.E01, status=CheckStatus.PASSED),
                CheckResult(check_id=CheckID.E08, status=CheckStatus.PASSED),
            ],
            output_path="/tmp/test",
            config=config,
        )
        assert report.all_passed is True
        assert report.passed == 2
        assert report.failed == 0

    def test_mixed_results(self) -> None:
        tiles = _make_tiles(1, 1)
        config = _make_config(tiles)
        report = ValidationReport(
            results=[
                CheckResult(check_id=CheckID.E01, status=CheckStatus.PASSED),
                CheckResult(check_id=CheckID.E08, status=CheckStatus.FAILED, message="bad"),
            ],
            output_path="/tmp/test",
            config=config,
        )
        assert report.all_passed is False
        assert report.passed == 1
        assert report.failed == 1

    def test_skipped_counts_as_passed(self) -> None:
        tiles = _make_tiles(1, 1)
        config = _make_config(tiles)
        report = ValidationReport(
            results=[
                CheckResult(check_id=CheckID.E01, status=CheckStatus.PASSED),
                CheckResult(check_id=CheckID.E03, status=CheckStatus.SKIPPED),
            ],
            output_path="/tmp/test",
            config=config,
        )
        assert report.all_passed is True

    def test_to_dict(self) -> None:
        tiles = _make_tiles(1, 1)
        config = _make_config(tiles)
        report = ValidationReport(
            results=[
                CheckResult(check_id=CheckID.E01, status=CheckStatus.PASSED, message="ok"),
            ],
            output_path="/tmp/test",
            config=config,
        )
        d = report.to_dict()
        assert d["summary"]["passed"] == 1
        assert d["results"][0]["check_id"] == "E01"
        assert d["results"][0]["status"] == "passed"

    def test_render_returns_panel(self) -> None:
        tiles = _make_tiles(1, 1)
        config = _make_config(tiles)
        report = ValidationReport(
            results=[
                CheckResult(check_id=CheckID.E01, status=CheckStatus.PASSED),
            ],
            output_path="/tmp/test",
            config=config,
        )
        panel = report.render()
        assert panel is not None


# ---------------------------------------------------------------------------
# Tile filename
# ---------------------------------------------------------------------------


class TestTileFilename:
    def test_format(self) -> None:
        tile = TileCoordinate(col_px=0, row_px=0, width_px=1, height_px=1, row=3, col=12)
        assert tile_filename(tile) == "tile_r0003_c0012.tif"

    def test_zero_padded(self) -> None:
        tile = TileCoordinate(col_px=0, row_px=0, width_px=1, height_px=1, row=0, col=0)
        assert tile_filename(tile) == "tile_r0000_c0000.tif"


# ---------------------------------------------------------------------------
# validate_output integration (top-level API)
# ---------------------------------------------------------------------------


class TestValidateOutput:
    def test_basic_pass(self, tmp_path: Path) -> None:
        from datensee.validation import validate_output

        tiles = _make_tiles(2, 2)
        config = _make_config(tiles, output_path=str(tmp_path))

        for t in tiles:
            _write_fake_tiff(tmp_path / tile_filename(t), size_bytes=8192)

        report = validate_output(tmp_path, config, checks=[CheckID.E01, CheckID.E08, CheckID.E10])
        # E01 and E08 should pass; E10 should pass (size is plausible)
        for r in report.results:
            if r.check_id in (CheckID.E01, CheckID.E08, CheckID.E10):
                assert r.status == CheckStatus.PASSED, f"{r.check_id}: {r.message}"

    def test_e07_without_credentials_errors(self, tmp_path: Path) -> None:
        from datensee.validation import validate_output

        tiles = _make_tiles(1, 1)
        config = _make_config(tiles, output_path=str(tmp_path))
        _write_fake_tiff(tmp_path / tile_filename(tiles[0]))

        # E07 requires API credentials — without them, it should error gracefully
        report = validate_output(tmp_path, config, checks=[CheckID.E07])
        assert report.results[0].status in (CheckStatus.ERROR, CheckStatus.SKIPPED)


# ===========================================================================
# Phase 2 tests: E03, E04, E05, E06, E09
# ===========================================================================


# ---------------------------------------------------------------------------
# Helpers for synthetic GeoTIFFs with metadata (mock rasterio)
# ---------------------------------------------------------------------------


def _make_tiff_info(
    width: int = 64,
    height: int = 64,
    band_count: int = 1,
    dtype: str = "float32",
    crs: str | None = "EPSG:4326",
    transform: tuple[float, ...] | None = None,
):
    """Build a TiffInfo for mocking."""
    from datensee.validation.tiff import TiffInfo

    return TiffInfo(
        width=width,
        height=height,
        band_count=band_count,
        dtype=dtype,
        crs=crs,
        transform=transform,
    )


# ---------------------------------------------------------------------------
# E03: Tile Geospatial Metadata
# ---------------------------------------------------------------------------


class TestE03TileGeospatialMetadata:
    def test_correct_metadata(self, tmp_path: Path) -> None:
        from datensee.validation.spatial import check_e03_tile_geospatial_metadata

        tiles = _make_tiles(1, 1)
        config = _make_config(tiles)
        tile = tiles[0]
        _write_fake_tiff(tmp_path / tile_filename(tile))

        # Mock read_tiff_info to return matching metadata
        info = _make_tiff_info(
            crs="EPSG:4326",
            transform=_transform_for(config, tile),
        )
        with patch("datensee.validation.spatial.read_tiff_info", return_value=info):
            result = check_e03_tile_geospatial_metadata(tmp_path, config, tiles)

        assert result.status == CheckStatus.PASSED

    def test_wrong_crs(self, tmp_path: Path) -> None:
        from datensee.validation.spatial import check_e03_tile_geospatial_metadata

        tiles = _make_tiles(1, 1)
        config = _make_config(tiles)
        tile = tiles[0]
        _write_fake_tiff(tmp_path / tile_filename(tile))

        info = _make_tiff_info(
            crs="EPSG:32610",
            transform=_transform_for(config, tile),
        )
        with patch("datensee.validation.spatial.read_tiff_info", return_value=info):
            result = check_e03_tile_geospatial_metadata(tmp_path, config, tiles)

        assert result.status == CheckStatus.FAILED
        assert "CRS" in result.message or "metadata" in result.message

    def test_wrong_origin(self, tmp_path: Path) -> None:
        from datensee.validation.spatial import check_e03_tile_geospatial_metadata

        tiles = _make_tiles(1, 1)
        config = _make_config(tiles)
        tile = tiles[0]
        _write_fake_tiff(tmp_path / tile_filename(tile))

        # Origin is offset by 1.0 in X — clearly wrong
        sx, shx, tx, shy, sy, ty = _transform_for(config, tile)
        info = _make_tiff_info(
            crs="EPSG:4326",
            transform=(sx, shx, tx + 1.0, shy, sy, ty),
        )
        with patch("datensee.validation.spatial.read_tiff_info", return_value=info):
            result = check_e03_tile_geospatial_metadata(tmp_path, config, tiles)

        assert result.status == CheckStatus.FAILED

    def test_missing_file_skipped(self, tmp_path: Path) -> None:
        from datensee.validation.spatial import check_e03_tile_geospatial_metadata

        tiles = _make_tiles(1, 1)
        config = _make_config(tiles)
        # Don't create the file — should pass with 0 checked
        result = check_e03_tile_geospatial_metadata(tmp_path, config, tiles)
        assert result.status == CheckStatus.PASSED  # 0 failures out of 0 checked


# ---------------------------------------------------------------------------
# E04: Boundary Continuity
# ---------------------------------------------------------------------------


class TestE04BoundaryContinuity:
    def test_continuous_boundaries(self, tmp_path: Path) -> None:
        from datensee.validation.spatial import check_e04_boundary_continuity

        tiles = _make_tiles(1, 2)  # Two side-by-side tiles
        config = _make_config(tiles)

        for t in tiles:
            _write_fake_tiff(tmp_path / tile_filename(t))

        # Mock pixels: left tile's right edge == right tile's left edge
        pixels = np.ones((64, 64), dtype=np.float32) * 100.0

        with patch("datensee.validation.spatial.read_tiff_pixels", return_value=pixels):
            result = check_e04_boundary_continuity(tmp_path, config, tiles)

        assert result.status == CheckStatus.PASSED

    def test_discontinuous_boundary(self, tmp_path: Path) -> None:
        from datensee.validation.spatial import check_e04_boundary_continuity

        tiles = _make_tiles(1, 2)
        config = _make_config(tiles)

        for t in tiles:
            _write_fake_tiff(tmp_path / tile_filename(t))

        # Mock: left tile pixels = 0, right tile pixels = 1000
        call_count = [0]

        def mock_read(path, band=1):
            call_count[0] += 1
            if Path(path).name == "tile_r0000_c0000.tif":
                return np.zeros((64, 64), dtype=np.float32)
            return np.full((64, 64), 1000.0, dtype=np.float32)

        with patch("datensee.validation.spatial.read_tiff_pixels", side_effect=mock_read):
            result = check_e04_boundary_continuity(tmp_path, config, tiles, threshold=50.0)

        assert result.status == CheckStatus.FAILED

    def test_no_neighbors_skipped(self, tmp_path: Path) -> None:
        from datensee.validation.spatial import check_e04_boundary_continuity

        tiles = _make_tiles(1, 1)  # Single tile, no neighbors
        config = _make_config(tiles)
        _write_fake_tiff(tmp_path / tile_filename(tiles[0]))

        result = check_e04_boundary_continuity(tmp_path, config, tiles)
        assert result.status == CheckStatus.SKIPPED


# ---------------------------------------------------------------------------
# E09: Pixel Range Sanity
# ---------------------------------------------------------------------------


class TestE09PixelRangeSanity:
    def test_values_in_range(self, tmp_path: Path) -> None:
        from datensee.validation.tile_integrity import check_e09_pixel_range_sanity

        tiles = _make_tiles(1, 2)
        config = _make_config(tiles)

        for t in tiles:
            _write_fake_tiff(tmp_path / tile_filename(t))

        # Normal float32 values
        pixels = np.random.default_rng(42).uniform(-100, 100, (64, 64)).astype(np.float32)
        with patch("datensee.validation.tile_integrity.read_tiff_pixels", return_value=pixels):
            result = check_e09_pixel_range_sanity(tmp_path, config, tiles)

        assert result.status == CheckStatus.PASSED

    def test_all_nan_tiles_fail(self, tmp_path: Path) -> None:
        from datensee.validation.tile_integrity import check_e09_pixel_range_sanity

        tiles = _make_tiles(1, 1)
        config = _make_config(tiles)
        _write_fake_tiff(tmp_path / tile_filename(tiles[0]))

        nan_pixels = np.full((64, 64), np.nan, dtype=np.float32)
        with patch("datensee.validation.tile_integrity.read_tiff_pixels", return_value=nan_pixels):
            result = check_e09_pixel_range_sanity(tmp_path, config, tiles)

        # 1/1 = 100% all-NaN → should fail (>5% threshold)
        assert result.status == CheckStatus.FAILED
        assert "all-NaN" in result.message

    def test_out_of_range_values(self, tmp_path: Path) -> None:
        from datensee.validation.tile_integrity import check_e09_pixel_range_sanity

        tiles = _make_tiles(1, 1)
        config = _make_config(tiles, data_type="uint8")
        _write_fake_tiff(tmp_path / tile_filename(tiles[0]))

        # Values outside uint8 range [0, 255]
        pixels = np.full((64, 64), 999.0, dtype=np.float64)
        with patch("datensee.validation.tile_integrity.read_tiff_pixels", return_value=pixels):
            result = check_e09_pixel_range_sanity(tmp_path, config, tiles)

        assert result.status == CheckStatus.FAILED
        assert "in range" in result.message

    def test_no_readable_tiles_skipped(self, tmp_path: Path) -> None:
        from datensee.validation.tile_integrity import check_e09_pixel_range_sanity

        tiles = _make_tiles(1, 1)
        config = _make_config(tiles)
        # No files on disk

        result = check_e09_pixel_range_sanity(tmp_path, config, tiles)
        assert result.status == CheckStatus.SKIPPED


# ---------------------------------------------------------------------------
# E07: Pixel Value Accuracy (mocked — no real API calls)
# ---------------------------------------------------------------------------


class TestE07PixelValueAccuracy:
    def test_matching_pixels(self, tmp_path: Path) -> None:
        from datensee.validation.reference import check_e07_pixel_value_accuracy

        tiles = _make_tiles(1, 2)
        config = _make_config(tiles)

        for t in tiles:
            _write_fake_tiff(tmp_path / tile_filename(t))

        pixels = np.random.default_rng(42).uniform(0, 100, (64, 64)).astype(np.float32)

        with (
            patch("datensee.validation.reference.read_tiff_pixels", return_value=pixels),
            patch("datensee.validation.reference._fetch_tile_as_numpy", return_value=pixels),
        ):
            result = check_e07_pixel_value_accuracy(
                tmp_path,
                config,
                tiles,
                gee_project="test",
                access_token="fake-token",
            )

        assert result.status == CheckStatus.PASSED

    def test_mismatched_pixels(self, tmp_path: Path) -> None:
        from datensee.validation.reference import check_e07_pixel_value_accuracy

        tiles = _make_tiles(1, 1)
        config = _make_config(tiles)
        _write_fake_tiff(tmp_path / tile_filename(tiles[0]))

        disk_pixels = np.zeros((64, 64), dtype=np.float32)
        ref_pixels = np.ones((64, 64), dtype=np.float32)

        with (
            patch("datensee.validation.reference.read_tiff_pixels", return_value=disk_pixels),
            patch("datensee.validation.reference._fetch_tile_as_numpy", return_value=ref_pixels),
        ):
            result = check_e07_pixel_value_accuracy(
                tmp_path,
                config,
                tiles,
                gee_project="test",
                access_token="fake-token",
            )

        assert result.status == CheckStatus.FAILED
        assert "differ" in result.message

    def test_no_tiles_skipped(self, tmp_path: Path) -> None:
        from datensee.validation.reference import check_e07_pixel_value_accuracy

        tiles = _make_tiles(1, 1)
        config = _make_config(tiles)
        # No files on disk

        result = check_e07_pixel_value_accuracy(
            tmp_path,
            config,
            tiles,
            gee_project="test",
            access_token="fake-token",
        )
        assert result.status == CheckStatus.SKIPPED

    def test_3d_reference_array(self, tmp_path: Path) -> None:
        """NPY from EE can be (bands, height, width) — E07 normalizes to 2D."""
        from datensee.validation.reference import check_e07_pixel_value_accuracy

        tiles = _make_tiles(1, 1)
        config = _make_config(tiles)
        _write_fake_tiff(tmp_path / tile_filename(tiles[0]))

        pixels_2d = np.ones((64, 64), dtype=np.float32) * 42.0
        pixels_3d = np.ones((1, 64, 64), dtype=np.float32) * 42.0

        with (
            patch("datensee.validation.reference.read_tiff_pixels", return_value=pixels_2d),
            patch("datensee.validation.reference._fetch_tile_as_numpy", return_value=pixels_3d),
        ):
            result = check_e07_pixel_value_accuracy(
                tmp_path,
                config,
                tiles,
                gee_project="test",
                access_token="fake-token",
            )

        assert result.status == CheckStatus.PASSED
