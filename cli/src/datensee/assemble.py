"""Post-processing: assemble per-tile GeoTIFFs into a GDAL VRT mosaic.

The Beam pipeline writes one GeoTIFF per tile. This module builds a GDAL
Virtual Raster (VRT) that mosaics them into a single logical raster without
copying any pixel data.

To convert the VRT to a COG:
    gdal_translate -of COG -co COMPRESS=LZW output.vrt output.tif
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from datensee.config import PipelineConfig, TileCoordinate

# Map config data_type values to GDAL VRT DataType names.
_DATA_TYPE_MAP: dict[str, str] = {
    "float32": "Float32",
    "float64": "Float64",
    "int16": "Int16",
    "int32": "Int32",
    "uint8": "Byte",
    "uint16": "UInt16",
}


def _vrt_data_type(config_type: str) -> str:
    """Convert a config data_type string to the VRT DataType attribute value."""
    return _DATA_TYPE_MAP.get(config_type, "Float32")


def write_vrt(config: PipelineConfig, output_dir: Path) -> Path:
    """Generate a GDAL VRT mosaic from the tile grid and per-tile GeoTIFFs.

    Args:
        config: Pipeline config describing the tile grid and output path.
        output_dir: Directory where per-tile GeoTIFFs were written.

    Returns:
        Path to the written .vrt file.
    """
    grid = config.tile_grid
    tile_px = grid.tile_size_pixels
    tiles = grid.tiles

    if not tiles:
        raise ValueError("Cannot build VRT: tile grid is empty")

    max_row = max(t.row for t in tiles)
    max_col = max(t.col for t in tiles)
    n_rows = max_row + 1
    n_cols = max_col + 1

    all_x_min = min(t.x_min for t in tiles)
    all_y_max = max(t.y_max for t in tiles)
    all_x_max = max(t.x_max for t in tiles)
    all_y_min = min(t.y_min for t in tiles)

    raster_x = n_cols * tile_px
    raster_y = n_rows * tile_px
    pixel_w = (all_x_max - all_x_min) / raster_x
    pixel_h = (all_y_max - all_y_min) / raster_y

    band_count = config.output.band_count
    data_type = _vrt_data_type(config.output.data_type)

    root = ET.Element("VRTDataset", rasterXSize=str(raster_x), rasterYSize=str(raster_y))

    ET.SubElement(root, "SRS").text = grid.crs

    ET.SubElement(root, "GeoTransform").text = (
        f"{all_x_min}, {pixel_w}, 0, {all_y_max}, 0, -{pixel_h}"
    )

    for band_idx in range(1, band_count + 1):
        band_el = ET.SubElement(
            root, "VRTRasterBand", dataType=data_type, band=str(band_idx)
        )
        ET.SubElement(band_el, "NoDataValue").text = "nan"

        for tile in tiles:
            tif_name = f"tile_r{tile.row:04d}_c{tile.col:04d}.tif"

            # VRT y=0 is at the top (north); our row=0 is at the south.
            dst_x = tile.col * tile_px
            dst_y = (max_row - tile.row) * tile_px

            src = ET.SubElement(band_el, "SimpleSource")
            ET.SubElement(src, "SourceFilename", relativeToVRT="1").text = tif_name
            ET.SubElement(src, "SourceBand").text = str(band_idx)
            ET.SubElement(
                src,
                "SourceProperties",
                RasterXSize=str(tile_px),
                RasterYSize=str(tile_px),
                DataType=data_type,
                BlockXSize=str(tile_px),
                BlockYSize="1",
            )
            ET.SubElement(
                src,
                "SrcRect",
                xOff="0",
                yOff="0",
                xSize=str(tile_px),
                ySize=str(tile_px),
            )
            ET.SubElement(
                src,
                "DstRect",
                xOff=str(dst_x),
                yOff=str(dst_y),
                xSize=str(tile_px),
                ySize=str(tile_px),
            )

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    vrt_path = output_dir / "mosaic.vrt"
    tree.write(vrt_path, xml_declaration=True, encoding="UTF-8")
    return vrt_path
