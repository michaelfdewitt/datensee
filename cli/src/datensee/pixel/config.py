"""Pydantic models for the pixel pipeline's shape of the config.

These are the raster-specific halves of the contract between the CLI
and the Beam pipeline: the parent :class:`PixelGrid` plus each tile
inside it, COG output parameters, and the output-config bundle. The
runner-agnostic envelope (``PipelineConfig``, ``RunnerConfig``,
``DataflowRunnerConfig``) lives in
:mod:`datensee.config` so a future vector pipeline can compose with
the same envelope.

The canonical export shape is :class:`PixelGrid` — CRS code + 6-tuple
affine transform + integer dimensions. It mirrors Earth Engine's own
``PixelGrid`` type so the parent grid (and per-tile sub-grids derived
from it) can be sent verbatim to the HV ``computePixels`` endpoint.

Tile geometry inside the export is integer pixel rectangles
(``col_px``/``row_px``/``width_px``/``height_px``) within that parent
grid. Float bboxes are derived from ``transform × pixel_offsets`` —
never persisted — so cross-export grid alignment is unconditional
whenever two exports share the same CRS, scale, and tile size.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class GridDimensions(BaseModel):
    """Pixel dimensions of a grid (mirrors EE's ``GridDimensions``)."""

    width: int = Field(gt=0)
    height: int = Field(gt=0)


class AffineTransform(BaseModel):
    """6-tuple geo-affine in the EE / GDAL / GeoTIFF convention.

    For axis-aligned grids (always, in our use), ``shear_x`` and ``shear_y``
    are zero, ``scale_x`` is positive, and ``scale_y`` is **negative** —
    that puts ``(translate_x, translate_y)`` at the NW corner of the
    top-left pixel (PixelIsArea: pixel ``(u=0, v=0)`` is the *corner*,
    not the centre).
    """

    scale_x: float
    shear_x: float = 0.0
    translate_x: float
    shear_y: float = 0.0
    scale_y: float
    translate_y: float


class PixelGrid(BaseModel):
    """Canonical export grid: CRS + affine + integer dimensions.

    Sent verbatim to EE's ``computePixels`` endpoint at fetch time.
    """

    crs_code: str = Field(description="EPSG code or proj string, e.g. 'EPSG:4326'")
    affine_transform: AffineTransform
    dimensions: GridDimensions


class TileCoordinate(BaseModel):
    """A compute tile as an integer pixel rectangle inside the parent grid.

    ``col_px``/``row_px`` are **local** offsets from the parent grid's
    ``translate_x``/``translate_y`` — not absolute against a global ``(0, 0)``.
    The parent's translate already encodes where the export sits in CRS units;
    tile offsets within it are small. ``width_px``/``height_px`` are the tile's
    own pixel dimensions; for root tiles they equal ``tile_size_pixels``, and
    quadtree split children halve each axis.

    ``row`` and ``col`` are the compute-tile indices within the export bbox
    (row=0 is the northernmost tile, col=0 the westernmost). They stay pinned
    to the *root* compute tile — split children inherit them so the failure
    journal can attribute children to the parent that originated them.

    ``out_row``/``out_col`` (two-tier tiling) identify the output tile this
    compute tile belongs to. When two-tier mode is disabled they equal
    ``row``/``col``.

    ``lineage`` records the quadtree path from the root compute tile down to a
    sub-tile. Each entry is a quadrant index 0–3, layout-independent of CRS
    axis order: ``0=x-low/y-low``, ``1=x-high/y-low``, ``2=x-low/y-high``,
    ``3=x-high/y-high``. Empty list = root compute tile. Lineage exists for
    the failure-journal / retry-decision logic.
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
        description="Quadtree path from root compute tile (each entry 0–3).",
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
        """CRS string — delegates to ``pixel_grid.crs_code``."""
        return self.pixel_grid.crs_code

    @property
    def pixel_size(self) -> float:
        """Pixel size in CRS units (== ``affine_transform.scale_x``)."""
        return self.pixel_grid.affine_transform.scale_x

    @model_validator(mode="after")
    def exactly_one_tile_source(self) -> TileGrid:
        has_inline = self.tiles is not None and len(self.tiles) > 0
        has_file = self.tiles_file is not None and self.tiles_file.strip() != ""
        if not has_inline and not has_file:
            raise ValueError("TileGrid must have either inline tiles or a tiles_file path")
        if has_inline and has_file:
            raise ValueError(
                "TileGrid cannot have both inline tiles and tiles_file — use one or the other"
            )
        return self


class OutputConfig(BaseModel):
    """Destination and format config for pipeline output.

    output_path accepts either a GCS URI (gs://bucket/prefix) or a local
    directory path for local-runner mode.

    output_tile_size_pixels (two-tier tiling) specifies the edge length
    of the *output* COGs. When unset or equal to the compute tile size
    (`tile_grid.tile_size_pixels`), each compute tile becomes its own
    output COG. When set to a multiple of the compute tile size, compute
    tiles are grouped and assembled into larger output COGs whose
    internal block size is the compute tile size. This decouples fetch
    parallelism from output file granularity.
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
            "tile_grid.tile_size_pixels. Defaults to tile_size_pixels (one COG per "
            "compute tile)."
        ),
    )
    merge_existing_output: bool = Field(
        default=False,
        description=(
            "Retry semantics: when True, the assembler decodes an "
            "already-existing output COG and overlays this run's tiles onto "
            "it, so re-fetched tiles (including quadtree split children) "
            "fill holes instead of the file being rebuilt from this run's "
            "tiles alone. Set by `datensee retry`; fresh exports leave this "
            "False so stale files are replaced, never blended into."
        ),
    )
    nodata: float | None = Field(
        default=None,
        description=(
            "Optional nodata value written as the GDAL_NODATA tag on every "
            "output COG. EE's computePixels returns masked pixels as 0 with "
            "no mask channel, so 'masked' and 'legitimately zero' are "
            "indistinguishable downstream — unmask(sentinel) the expression "
            "and declare the sentinel here so GIS tools treat it as nodata."
        ),
    )
    compression: Literal["deflate", "none"] = Field(
        default="deflate",
        description=(
            "COG compression. The transcoder's only real knob: no overviews, "
            "no predictor, and the internal block size is always the compute "
            "tile size — features the pipeline doesn't implement don't "
            "belong on the config surface."
        ),
    )


class PixelPayload(BaseModel):
    """Pixel-pipeline payload nested under :class:`PipelineConfig.pixel`.

    Holds the raster-specific work-unit description: the parent tile grid
    and the output config that says where COGs land and how they're packed.
    A future vector pipeline plugs a sibling ``VectorPayload`` onto the
    same envelope (selected via ``PipelineConfig.pipeline_kind``); both
    pixel and vector keep their own ``output`` shape — there's no shared
    output type, just a shared envelope.
    """

    tile_grid: TileGrid
    output: OutputConfig
