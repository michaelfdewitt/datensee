"""Pixel pipeline: raster-shaped subpackage for the EE `computePixels` path.

This subpackage is the home for everything that's tied to the pixel-grid
model — tile decomposition, COG output, raster validation, the quadtree
retry splitter, and the Pydantic models that describe a raster export
(``PixelGrid``, ``TileCoordinate``, ``TileGrid``, ``OutputConfig``,
``OutputConfig``). The top-level :mod:`datensee` package keeps the
shared infrastructure (auth, snapshot pinning, Dataflow submission,
job polling, the runner-agnostic envelope of ``PipelineConfig``).

The seam exists so a future vector pipeline (``computeFeatures`` →
zonal stats → GeoParquet) can land as a sibling subpackage without
disturbing the pixel surface, and so a reader of either subpackage
sees only the code that's relevant to it.
"""

from __future__ import annotations
