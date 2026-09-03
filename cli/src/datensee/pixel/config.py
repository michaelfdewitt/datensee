"""Pydantic models for the pixel pipeline configuration.

Covers raster-specific models for the CLI and Beam pipeline: parent PixelGrid,
TileCoordinate records, COG output parameters, and output configuration.
Envelope models (PipelineConfig, RunnerConfig, DataflowRunnerConfig) live in
datensee.config.

The export grid follows Earth Engine's PixelGrid specification: CRS code,
6-element affine transform, and integer dimensions in pixels. Tile geometry
is specified as integer pixel rectangles within the parent grid. CRS bboxes
are computed from transform and pixel offsets when needed.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class GridDimensions(BaseModel):
    """Pixel dimensions of a grid."""

    width: int = Field(gt=0)
    height: int = Field(gt=0)


class AffineTransform(BaseModel):
    """6-element affine transform in GDAL/GeoTIFF order.

    For axis-aligned grids, scale_x is positive and scale_y is negative,
    placing (translate_x, translate_y) at the NW pixel corner (PixelIsArea).
    """

    scale_x: float
    shear_x: float = 0.0
    translate_x: float
    shear_y: float = 0.0
    scale_y: float
    translate_y: float

    @model_validator(mode="after")
    def _validate_non_singular(self) -> AffineTransform:
        det = self.scale_x * self.scale_y - self.shear_x * self.shear_y
        if math.isclose(det, 0.0, abs_tol=1e-12):
            raise ValueError(f"AffineTransform is singular or degenerate (determinant={det})")
        return self


class PixelGrid(BaseModel):
    """Export grid definition (CRS + affine transform + pixel dimensions)."""

    crs_code: str = Field(description="EPSG code or proj string, e.g. 'EPSG:4326'")
    affine_transform: AffineTransform
    dimensions: GridDimensions


class TileCoordinate(BaseModel):
    """Compute tile defined as an integer pixel rectangle in the parent grid.

    col_px and row_px are local offsets from parent grid translate_x/y.
    width_px and height_px are the tile dimensions.

    row and col are tile indices in the export grid. In two-tier mode,
    out_row and out_col identify the output COG container.
    """

    col_px: int = Field(ge=0)
    row_px: int = Field(ge=0)
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    row: int = 0
    col: int = 0
    out_row: int = 0
    out_col: int = 0
    lineage: list[int] = Field(
        default_factory=list,
        description="Quadtree path from root compute tile (each entry 0-3).",
    )


class TileGrid(BaseModel):
    """Tile decomposition of the export region.

    Carries the parent :class:`PixelGrid` plus the tile size and either inline
    tiles or a path to an NDJSON tile file.
    """

    pixel_grid: PixelGrid
    tile_size_pixels: int = Field(default=512, gt=0, description="Tile edge length in pixels")
    tiles: list[TileCoordinate] | None = Field(
        default=None,
        description="Inline tile coordinates (mutually exclusive with tiles_file)",
    )
    tiles_file: str | None = Field(
        default=None,
        description="GCS URI or local path to NDJSON file of tile coordinates",
    )

    @property
    def crs(self) -> str:
        """CRS string, delegating to pixel_grid.crs_code."""
        return self.pixel_grid.crs_code

    @property
    def pixel_size(self) -> float:
        """Pixel size in CRS units."""
        return self.pixel_grid.affine_transform.scale_x

    @model_validator(mode="after")
    def exactly_one_tile_source(self) -> TileGrid:
        has_inline = self.tiles is not None and len(self.tiles) > 0
        has_file = self.tiles_file is not None and self.tiles_file.strip() != ""
        if not has_inline and not has_file:
            raise ValueError("TileGrid must have either inline tiles or a tiles_file path")
        if has_inline and has_file:
            raise ValueError("TileGrid cannot have both inline tiles and tiles_file: specify one.")
        return self


class OutputConfig(BaseModel):
    """Destination and format config for pipeline output.

    output_path accepts a GCS URI (gs://bucket/prefix) or local directory path.
    """

    output_path: str = Field(description="Output path: GCS URI (gs://…) or local directory")
    band_count: int = Field(default=1, gt=0, description="Number of output bands")
    data_type: Literal["float32", "float64", "int16", "int32", "uint8", "uint16"] = Field(
        default="float32", description="Pixel data type for output raster"
    )
    output_tile_size_pixels: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Edge length of output tile COGs in pixels. Must be a multiple of "
            "tile_grid.tile_size_pixels. Defaults to tile_size_pixels."
        ),
    )
    merge_existing_output: bool = Field(
        default=False,
        description=(
            "Retry semantics: when True, overlay tiles onto existing output COGs "
            "instead of rebuilding from this run's tiles alone."
        ),
    )
    nodata: float | None = Field(
        default=None,
        description=(
            "Optional nodata value written as GDAL_NODATA tag. EE returns masked "
            "pixels as 0 with no mask channel; unmask(sentinel) the expression "
            "and declare the sentinel here so GIS tools treat it as nodata."
        ),
    )
    compression: Literal["deflate", "none"] = Field(
        default="deflate",
        description="COG compression ('deflate' or 'none').",
    )


class PixelPayload(BaseModel):
    """Pixel-pipeline payload nested under PipelineConfig.pixel."""

    tile_grid: TileGrid
    output: OutputConfig

    tile_grid: TileGrid
    output: OutputConfig
