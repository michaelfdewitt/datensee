"""Tests for VRT mosaic assembly."""

from __future__ import annotations

import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from datensee.assemble import write_vrt
from datensee.config import (
    OutputConfig,
    PipelineConfig,
    TileCoordinate,
    TileGrid,
)


def _make_config(
    band_count: int = 1,
    data_type: str = "float32",
) -> PipelineConfig:
    return PipelineConfig(
        ee_expression='{"result":"0","values":{}}',
        gee_project="test-project",
        tile_grid=TileGrid(
            crs="EPSG:4326",
            scale_meters=30.0,
            tile_size_pixels=256,
            tiles=[
                TileCoordinate(x_min=0, y_min=0, x_max=1, y_max=1, row=0, col=0),
                TileCoordinate(x_min=1, y_min=0, x_max=2, y_max=1, row=0, col=1),
            ],
        ),
        output=OutputConfig(
            output_path="/tmp/test",
            band_count=band_count,
            data_type=data_type,
        ),
    )


def test_single_band_vrt() -> None:
    config = _make_config(band_count=1)
    with tempfile.TemporaryDirectory() as tmpdir:
        vrt_path = write_vrt(config, Path(tmpdir))
        tree = ET.parse(vrt_path)
        root = tree.getroot()

        bands = root.findall("VRTRasterBand")
        assert len(bands) == 1
        assert bands[0].get("band") == "1"
        assert bands[0].get("dataType") == "Float32"

        sources = bands[0].findall("SimpleSource")
        assert len(sources) == 2


def test_multiband_vrt() -> None:
    config = _make_config(band_count=3, data_type="uint8")
    with tempfile.TemporaryDirectory() as tmpdir:
        vrt_path = write_vrt(config, Path(tmpdir))
        tree = ET.parse(vrt_path)
        root = tree.getroot()

        bands = root.findall("VRTRasterBand")
        assert len(bands) == 3

        for i, band in enumerate(bands, start=1):
            assert band.get("band") == str(i)
            assert band.get("dataType") == "Byte"  # uint8 → Byte in GDAL

            sources = band.findall("SimpleSource")
            assert len(sources) == 2
            for src in sources:
                assert src.find("SourceBand").text == str(i)


def test_vrt_data_types() -> None:
    for config_type, vrt_type in [
        ("float32", "Float32"),
        ("float64", "Float64"),
        ("int16", "Int16"),
        ("int32", "Int32"),
        ("uint16", "UInt16"),
    ]:
        config = _make_config(data_type=config_type)
        with tempfile.TemporaryDirectory() as tmpdir:
            vrt_path = write_vrt(config, Path(tmpdir))
            tree = ET.parse(vrt_path)
            band = tree.getroot().find("VRTRasterBand")
            assert band.get("dataType") == vrt_type, (
                f"data_type={config_type} should produce VRT DataType={vrt_type}"
            )


def test_vrt_crs_from_config() -> None:
    config = _make_config()
    with tempfile.TemporaryDirectory() as tmpdir:
        vrt_path = write_vrt(config, Path(tmpdir))
        tree = ET.parse(vrt_path)
        srs = tree.getroot().find("SRS")
        assert srs.text == "EPSG:4326"


def test_empty_tiles_raises() -> None:
    config = PipelineConfig(
        ee_expression='{"result":"0","values":{}}',
        gee_project="test-project",
        tile_grid=TileGrid(
            crs="EPSG:4326",
            scale_meters=30.0,
            tiles=[TileCoordinate(x_min=0, y_min=0, x_max=1, y_max=1, row=0, col=0)],
        ),
        output=OutputConfig(output_path="/tmp/test"),
    )
    # Manually empty the tiles to bypass Pydantic validation
    config.tile_grid.tiles.clear()
    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(ValueError, match="empty"):
            write_vrt(config, Path(tmpdir))
